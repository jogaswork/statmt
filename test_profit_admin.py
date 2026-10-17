import asyncio
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

from telegram.error import BadRequest
import profits
import profit_admin as admin
import work


class ProfitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.patches = [patch.object(profits, "PATH", root / "profits.json"),
                        patch.object(work, "DATA_FILE", root / "top.json"),
                        patch.object(work, "RESTORE_MARKER", root / ".restore")]
        for item in self.patches: item.start()
        self.post = {"channel": -100123, "message": 12,
                     "values": {"worker": "@Alice", "amount": 200, "share": 100,
                                "service": "A", "tp": "—", "curator": "—"},
                     "template": "{worker}: {amount}, {share}", "status": "waiting"}
        profits.save({"history": [self.post]})
        work.save_data({"#ALICE": 333476, "@historic": 84323})
        self.bot = NS(edit_message_text=AsyncMock(), edit_message_caption=AsyncMock(),
                      edit_message_reply_markup=AsyncMock(), copy_message=AsyncMock(return_value=NS(message_id=99)))
        self.message = NS(chat=NS(type="private"), reply_text=AsyncMock(), photo=[],
                          message_id=8, chat_id=1)
        self.context = NS(bot=self.bot, bot_data={"profit_admins": {1}}, user_data={})

    def tearDown(self):
        for item in reversed(self.patches): item.stop()
        self.temp.cleanup()

    def change(self, **values):
        data = profits.get(); post = data["history"][0]
        new = dict(post["values"], **values)
        admin.persist_change(data, post, new)

    def test_delta_preserves_historical_total_and_alias(self):
        self.change(share=140)
        self.assertEqual(work.load_data(), {"#ALICE": 333516, "@historic": 84323})
        self.change(share=140)
        self.assertEqual(work.load_data()["#ALICE"], 333516)
        self.change(worker="#Bob", share=80)
        self.assertEqual(work.load_data(), {"#ALICE": 333376, "@historic": 84323, "@Bob": 80})

    def test_status_and_body_never_change_top(self):
        before = work.load_data()
        for status in admin.STATUSES:
            data = profits.get(); post = data["history"][0]; post["status"] = status
            admin.persist_change(data, post)
            self.assertEqual(work.load_data(), before)

    def test_single_status_button_has_matching_emoji(self):
        for status, label in admin.STATUSES.items():
            markup = admin.status_keyboard({"status": status})
            self.assertEqual(len(markup.inline_keyboard), 1)
            self.assertEqual(len(markup.inline_keyboard[0]), 1)
            button = markup.inline_keyboard[0][0]
            self.assertEqual(button.text, label)
            self.assertEqual(button.callback_data, "pf_status_" + status)
        self.assertEqual(admin.status_keyboard({}).inline_keyboard[0][0].text, "⏳ Ожидание")
        self.assertEqual(admin.status_keyboard({"status": "unknown"}).inline_keyboard[0][0].text, "⏳ Ожидание")

    async def test_startup_replaces_three_buttons_with_current_status(self):
        data = profits.get()
        data["history"][0].update(status="paid", buttons_version=1)
        profits.save(data)
        before = work.load_data()
        await admin.sync_history(NS(bot=self.bot))
        kwargs = self.bot.edit_message_reply_markup.await_args.kwargs
        self.assertEqual(kwargs["reply_markup"].inline_keyboard[0][0].text, "✅ Выплачено")
        self.assertEqual(sum(len(row) for row in kwargs["reply_markup"].inline_keyboard), 1)
        self.assertEqual(profits.get()["history"][0]["buttons_version"], admin.BUTTONS_VERSION)
        await admin.sync_history(NS(bot=self.bot))
        self.bot.edit_message_reply_markup.assert_awaited_once()
        self.assertEqual(work.load_data(), before)

    def test_crash_replay_is_idempotent(self):
        data = profits.get(); post = data["history"][0]
        new = dict(post["values"], share=150)
        with patch.object(profits, "save", side_effect=OSError("disk")):
            with self.assertRaises(OSError): admin.persist_change(data, post, new)
        self.assertTrue((work.DATA_FILE.parent / "profit_transaction.json").exists())
        self.assertEqual(work.load_data()["#ALICE"], 333526)
        self.assertEqual(profits.get()["history"][0]["values"]["share"], 150)
        self.assertEqual(work.load_data()["#ALICE"], 333526)
        self.assertFalse((work.DATA_FILE.parent / "profit_transaction.json").exists())

    def test_corrupt_files_fail_closed(self):
        profits.PATH.write_text("invalid")
        with self.assertRaises(ValueError): profits.get()
        work.DATA_FILE.write_text("invalid")
        with self.assertRaises(ValueError): work.load_data()

    async def test_public_status_buttons_are_silent_for_everyone(self):
        for user in (1, 2):
            query = NS(data="pf_status_paid", from_user=NS(id=user), answer=AsyncMock())
            await profits.callback(NS(callback_query=query), self.context)
            query.answer.assert_awaited_once_with()
        self.assertEqual(profits.get()["history"][0]["status"], "waiting")
        self.bot.edit_message_text.assert_not_awaited()

    async def test_nonadmin_cannot_manage(self):
        query = NS(data="pf_manage_status_paid", from_user=NS(id=2), answer=AsyncMock())
        await profits.callback(NS(callback_query=query), self.context)
        query.answer.assert_awaited_once_with()
        self.assertEqual(profits.get()["history"][0]["status"], "waiting")

    async def test_failed_channel_sync_retry_no_accrual(self):
        self.change(share=130)
        self.bot.edit_message_text.side_effect = BadRequest("forbidden")
        result = await admin.sync_saved(self.bot, admin.identity(self.post))
        self.assertIn("Сохранено", result)
        self.assertTrue(profits.get()["history"][0]["pending_sync"])
        self.bot.edit_message_text.side_effect = None
        await admin.sync_saved(self.bot, admin.identity(self.post))
        await admin.sync_saved(self.bot, admin.identity(self.post))
        self.assertFalse(profits.get()["history"][0]["pending_sync"])
        self.assertEqual(work.load_data()["#ALICE"], 333506)

    async def test_photo_edit_and_unchanged_telegram_response(self):
        post = dict(self.post, photo="file123")
        self.bot.edit_message_caption.side_effect = BadRequest("Message is not modified")
        await admin.sync_post(self.bot, post)
        self.assertEqual(self.bot.edit_message_caption.await_args.kwargs["caption"], "@Alice: 200, 100")
        self.bot.edit_message_text.assert_not_awaited()

    async def test_startup_migrates_legacy_buttons_without_touching_top(self):
        data = profits.get(); del data["history"][0]["template"]; profits.save(data)
        before = work.load_data()
        await admin.sync_history(NS(bot=self.bot))
        await admin.sync_history(NS(bot=self.bot))
        self.bot.edit_message_reply_markup.assert_awaited_once()
        self.assertEqual(work.load_data(), before)

    async def test_stale_editor_rejected(self):
        self.context.user_data["profit_flow"] = {"mode": "manage_input", "key": admin.identity(self.post),
                                                 "field": "share", "revision": 0}
        self.change(share=120)
        await admin.input_message(NS(text="180", reply_text=AsyncMock()), self.context)
        self.assertEqual(profits.get()["history"][0]["values"]["share"], 120)

    async def test_panel_buttons_bound_to_post_and_revision(self):
        await admin.show_post(self.message, self.context, admin.identity(self.post))
        markup = self.message.reply_text.await_args.kwargs["reply_markup"]
        actions = [button.callback_data for row in markup.inline_keyboard for button in row]
        self.assertTrue(all(len(action.encode()) <= 64 for action in actions))
        paid = next(action for action in actions if action.startswith("pf_manage_status_paid"))
        self.change(share=120)
        await admin.callback(NS(data=paid, message=self.message), self.context)
        self.assertEqual(profits.get()["history"][0]["status"], "waiting")
        markup = self.message.reply_text.await_args.kwargs["reply_markup"]
        paid = next(b.callback_data for row in markup.inline_keyboard for b in row
                    if b.callback_data.startswith("pf_manage_status_paid"))
        await admin.callback(NS(data=paid, message=self.message), self.context)
        self.assertEqual(profits.get()["history"][0]["status"], "paid")
        self.assertEqual(work.load_data()["#ALICE"], 333496)

    async def test_edit_input_updates_channel_content_and_delta(self):
        self.context.user_data["profit_flow"] = {"mode": "manage_input", "key": admin.identity(self.post),
                                                 "field": "share", "revision": 0}
        message = NS(text="175", reply_text=AsyncMock())
        await admin.input_message(message, self.context)
        self.assertEqual(self.bot.edit_message_text.await_args.kwargs["text"], "@Alice: 200, 175")
        self.assertEqual(work.load_data()["#ALICE"], 333551)
        await admin.input_message(message, self.context)
        self.assertEqual(work.load_data()["#ALICE"], 333551)

    async def test_legacy_forward_recovers_caption_without_accrual(self):
        data = profits.get(); del data["history"][0]["template"]; profits.save(data)
        self.context.user_data["profit_flow"] = {"mode": "manage_search"}
        message = NS(text=None, caption="Old <profit>", caption_entities=[], photo=[NS(file_id="photo")],
                     forward_origin=NS(chat=NS(id=-100123), message_id=12), reply_text=AsyncMock())
        await admin.input_message(message, self.context)
        post = profits.get()["history"][0]
        self.assertEqual(post["photo"], "photo")
        self.assertEqual(post["template"], "Old &lt;profit&gt;")
        self.assertEqual(work.load_data()["#ALICE"], 333476)

    def test_historical_snapshot_never_reappears_after_legitimate_delta(self):
        work.restore_historical_snapshot_once()
        self.assertTrue(work.RESTORE_MARKER.exists())
        work.save_data({"@historic": 100})
        work.restore_historical_snapshot_once()
        self.assertEqual(work.load_data(), {"@historic": 100})

    async def test_publish_duplicate_callback_only_copies_and_accrues_once(self):
        data = profits.get()
        data["templates"] = [{"body": "{worker}: {share}"}]
        data["channels"] = [{"id": -100123}]
        profits.save(data)
        self.context.user_data["profit_flow"] = {"mode": "confirm", "template": 0, "channel": 0,
            "preview_id": 8, "preview_chat": 1, "values": deepcopy(self.post["values"])}
        query = NS(data="pf_publish", from_user=NS(id=1), answer=AsyncMock(), message=self.message,
                   edit_message_reply_markup=AsyncMock())
        for _ in range(2): await profits.callback(NS(callback_query=query), self.context)
        self.bot.copy_message.assert_awaited_once()
        self.assertEqual(work.load_data()["#ALICE"], 333576)
        self.assertEqual(len(profits.get()["history"]), 2)
        self.assertIn("reply_markup", self.bot.copy_message.await_args.kwargs)

    async def test_search_paginates_and_uses_channel_message_identity(self):
        data = profits.get()
        data["history"] = [dict(deepcopy(self.post), message=i) for i in range(20)]
        profits.save(data)
        await admin.show_list(self.message, self.context)
        markup = self.message.reply_text.await_args.kwargs["reply_markup"]
        keys = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertEqual(sum(k.startswith("pf_manage_open_") for k in keys), 8)
        self.assertIn("pf_manage_open_-100123:19", keys)
        self.context.user_data["profit_search"] = "missing"
        await admin.show_list(self.message, self.context)
        self.assertIn("0 ·", self.message.reply_text.await_args.args[0])


if __name__ == "__main__": unittest.main()

