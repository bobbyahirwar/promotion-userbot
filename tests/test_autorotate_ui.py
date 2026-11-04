import asyncio
import base64
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bson import ObjectId

try:
    asyncio.get_running_loop()
    _IMPORT_LOOP = None
except RuntimeError:
    _IMPORT_LOOP = asyncio.new_event_loop()
    asyncio.set_event_loop(_IMPORT_LOOP)

os.environ.update({
    "BOT_TOKEN": "123:validation-token",
    "API_ID": "1",
    "API_HASH": "validation-hash",
    "SESSION_STRING": base64.urlsafe_b64encode(b"x" * 271).decode().rstrip("="),
    "OWNER_ID": "1",
    "MONGO_URI": "mongodb://127.0.0.1:27017/promotionbot",
})

from handlers import autorotate as ui

if _IMPORT_LOOP:
    _IMPORT_LOOP.close()
    asyncio.set_event_loop(None)


class _MemoryCursor:
    def __init__(self, documents):
        self.documents = list(documents)

    def sort(self, key, direction=1):
        self.documents.sort(key=lambda document: document.get(key), reverse=direction < 0)
        return self

    async def to_list(self, length):
        return self.documents if length is None else self.documents[:length]


class _MemoryCollection:
    def __init__(self, documents):
        self.documents = documents

    def find(self, query=None, projection=None):
        query = query or {}
        selected = self.documents
        for key, expected in query.items():
            if isinstance(expected, dict) and "$in" in expected:
                selected = [document for document in selected if document.get(key) in expected["$in"]]
            else:
                selected = [document for document in selected if document.get(key) == expected]
        return _MemoryCursor(selected)


class InlineAutoRotateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        ui._ui_drafts.clear()
        ui._custom_interval_inputs.clear()
        ui._callback_ids.clear()
        ui._callback_order.clear()
        ui._callback_locks.clear()

    def test_multi_select_select_all_clear_and_pagination(self):
        selection = ui._toggle_selection([1], 2)
        self.assertEqual(selection, [1, 2])
        self.assertEqual(ui._toggle_selection(selection, 1), [2])
        self.assertEqual(ui._select_all([1], [1, 2, 3]), [1, 2, 3])
        self.assertEqual(ui._clear_selection([1, 2, 4], [1, 2, 3]), [4])
        self.assertEqual(ui._page_bounds(21, 0), (0, 8, 3))
        self.assertEqual(ui._page_bounds(21, 2), (16, 21, 3))
        self.assertLessEqual(len(ui._callback("gt", 7, 2).encode()), 64)

    async def test_main_command_displays_four_inline_menu_actions(self):
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(effective_message=message)
        await ui.cmd_autorotate_menu(update, SimpleNamespace())
        args, kwargs = message.reply_text.await_args
        self.assertEqual(args[0], "Auto Message Rotation")
        markup = kwargs["reply_markup"]
        labels = [button.text for row in markup.inline_keyboard for button in row]
        self.assertIn("➕ Create New Configuration", labels)
        self.assertIn("🗂 Manage Configurations", labels)
        self.assertIn("🟢 Active Configurations", labels)
        self.assertIn("❔ Help", labels)

    async def test_group_toggle_and_select_all_use_callback_selection_state(self):
        ui._ui_drafts[1] = {
            "mode": "new",
            "name": "Rotation",
            "group_ids": [],
            "message_ids": [],
            "interval_seconds": 300,
        }
        groups = [{"chat_id": 101}, {"chat_id": 202}]
        query = SimpleNamespace(
            id="toggle",
            data="ar:gt:101:0",
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
            from_user=SimpleNamespace(id=1),
        )
        update = SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(id=1),
        )
        with patch.object(ui, "_load_pickable_groups", new=AsyncMock(return_value=(groups, set()))):
            await ui.callback_autorotate(update, SimpleNamespace())
        self.assertEqual(ui._ui_drafts[1]["group_ids"], [101])

        query.id = "select-all"
        query.data = "ar:ga:0"
        with patch.object(ui, "_load_pickable_groups", new=AsyncMock(return_value=(groups, set()))):
            await ui.callback_autorotate(update, SimpleNamespace())
        self.assertEqual(ui._ui_drafts[1]["group_ids"], [101, 202])

        query.id = "clear"
        query.data = "ar:gc:0"
        with patch.object(ui, "_load_pickable_groups", new=AsyncMock(return_value=(groups, set()))):
            await ui.callback_autorotate(update, SimpleNamespace())
        self.assertEqual(ui._ui_drafts[1]["group_ids"], [])

    async def test_message_toggle_and_select_all_use_callback_selection_state(self):
        messages = [
            {"_id": "message-1", "type": "text", "text": "first"},
            {"_id": "message-2", "type": "text", "text": "second"},
        ]
        ui._ui_drafts[1] = {
            "mode": "new",
            "name": "Rotation",
            "group_ids": [101],
            "message_ids": [],
            "interval_seconds": 300,
        }
        query = SimpleNamespace(
            id="message-toggle",
            data="ar:mt:message-1:0",
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
            from_user=SimpleNamespace(id=1),
        )
        update = SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(id=1),
        )
        with patch.object(ui, "_load_pickable_messages", new=AsyncMock(return_value=messages)):
            await ui.callback_autorotate(update, SimpleNamespace())
        self.assertEqual(ui._ui_drafts[1]["message_ids"], ["message-1"])

        query.id = "message-all"
        query.data = "ar:ma:0"
        with patch.object(ui, "_load_pickable_messages", new=AsyncMock(return_value=messages)):
            await ui.callback_autorotate(update, SimpleNamespace())
        self.assertEqual(ui._ui_drafts[1]["message_ids"], ["message-1", "message-2"])

        query.id = "message-clear"
        query.data = "ar:mc:0"
        with patch.object(ui, "_load_pickable_messages", new=AsyncMock(return_value=messages)):
            await ui.callback_autorotate(update, SimpleNamespace())
        self.assertEqual(ui._ui_drafts[1]["message_ids"], [])

    async def test_group_picker_has_pagination_and_selection_counts(self):
        groups = [{"chat_id": index, "title": f"Group {index}"} for index in range(17)]
        ui._ui_drafts[1] = {
            "mode": "new",
            "name": "Rotation",
            "group_ids": [1, 2],
            "message_ids": [],
            "interval_seconds": 300,
        }
        query = SimpleNamespace(edit_message_text=AsyncMock())
        with patch.object(ui, "_load_pickable_groups", new=AsyncMock(return_value=(groups, set()))):
            await ui._show_group_picker(query, 1, page=1)
        _, kwargs = query.edit_message_text.await_args
        self.assertIn("2 selected", kwargs["text"])
        markup = kwargs["reply_markup"]
        buttons = [button for row in markup.inline_keyboard for button in row]
        self.assertTrue(any(button.text == "2/3" for button in buttons))
        self.assertEqual(sum(button.callback_data.startswith("ar:gt:") for button in buttons), 8)

    async def test_message_picker_has_pagination_and_selection_count(self):
        messages = [
            {"_id": f"message-{index}", "type": "text", "text": f"saved {index}"}
            for index in range(17)
        ]
        ui._ui_drafts[1] = {
            "mode": "new",
            "name": "Rotation",
            "group_ids": [101],
            "message_ids": ["message-1", "message-2"],
            "interval_seconds": 300,
        }
        query = SimpleNamespace(edit_message_text=AsyncMock())
        with patch.object(ui, "_load_pickable_messages", new=AsyncMock(return_value=messages)):
            await ui._show_message_picker(query, 1, page=2)
        _, kwargs = query.edit_message_text.await_args
        self.assertIn("2 selected", kwargs["text"])
        buttons = [
            button
            for row in kwargs["reply_markup"].inline_keyboard
            for button in row
        ]
        self.assertTrue(any(button.text == "3/3" for button in buttons))
        self.assertEqual(sum(button.callback_data.startswith("ar:mt:") for button in buttons), 1)

    async def test_non_owner_callback_is_rejected(self):
        query = SimpleNamespace(id="unauthorized", answer=AsyncMock())
        update = SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(id=999),
        )
        await ui.callback_autorotate(update, SimpleNamespace())
        query.answer.assert_awaited_once()
        self.assertTrue(query.answer.await_args.kwargs["show_alert"])
        self.assertIn("Only the bot owner", query.answer.await_args.kwargs["text"])

    async def test_interval_button_updates_new_draft_before_confirmation(self):
        draft = {
            "mode": "new",
            "name": "Rotation",
            "group_ids": [101],
            "message_ids": ["message-id"],
            "interval_seconds": 300,
        }
        ui._ui_drafts[1] = draft
        query = SimpleNamespace(
            id="interval",
            data="ar:is:10",
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
            from_user=SimpleNamespace(id=1),
        )
        update = SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(id=1),
        )
        with patch.object(ui, "_show_creation_summary", new=AsyncMock()) as summary:
            await ui.callback_autorotate(update, SimpleNamespace())
        self.assertEqual(draft["interval_seconds"], 10)
        summary.assert_awaited_once_with(query, draft)

    async def test_interval_button_forwards_selected_seconds_to_backend(self):
        ui._ui_drafts[1] = {
            "mode": "edit_interval",
            "config_id": "config-id",
            "group_ids": [101],
            "message_ids": ["message-id"],
            "interval_seconds": 300,
        }
        query = SimpleNamespace(
            id="interval-edit",
            data="ar:is:20",
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
            from_user=SimpleNamespace(id=1),
        )
        update = SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(id=1),
        )
        with (
            patch.object(ui.autorotate, "edit_config", new=AsyncMock(return_value=True)) as edit,
            patch.object(ui, "_show_configuration", new=AsyncMock()),
        ):
            await ui.callback_autorotate(update, SimpleNamespace())
        edit.assert_awaited_once_with("config-id", {"interval_seconds": 20})

    async def test_five_second_interval_preset_is_saved_and_shown_in_summary(self):
        ui._ui_drafts[1] = {
            "mode": "edit_interval",
            "config_id": "config-id",
            "interval_seconds": 300,
        }
        query = SimpleNamespace(
            id="interval-five",
            data="ar:is:5",
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
            from_user=SimpleNamespace(id=1),
        )
        update = SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(id=1),
        )
        with (
            patch.object(ui.autorotate, "edit_config", new=AsyncMock(return_value=True)) as edit,
            patch.object(ui, "_show_configuration", new=AsyncMock()),
        ):
            await ui.callback_autorotate(update, SimpleNamespace())
        edit.assert_awaited_once_with("config-id", {"interval_seconds": 5})
        self.assertEqual(ui._interval_label(5), "5 seconds")

    async def test_creation_summary_formats_the_selected_interval(self):
        draft = {
            "name": "Rotation",
            "config_id": "config-id",
            "group_ids": [101],
            "message_ids": ["message-id"],
            "interval_seconds": 5,
        }
        query = SimpleNamespace(edit_message_text=AsyncMock())
        with patch.object(ui, "_save_new_config", new=AsyncMock()):
            await ui._show_creation_summary(query, draft)
        self.assertIn("Interval: 5 seconds", query.edit_message_text.await_args.kwargs["text"])

    async def test_custom_interval_below_ten_is_rejected_with_preset_guidance(self):
        ui._ui_drafts[1] = {
            "mode": "edit_interval",
            "config_id": "config-id",
            "interval_seconds": 300,
        }
        ui._custom_interval_inputs[1] = {"chat_id": 1, "message_id": 55}
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=1),
            effective_message=SimpleNamespace(text="5", chat_id=1),
        )
        bot = SimpleNamespace(edit_message_text=AsyncMock())
        with patch.object(ui.autorotate, "edit_config", new=AsyncMock()) as edit:
            await ui._handle_custom_interval(update, SimpleNamespace(bot=bot))
        edit.assert_not_awaited()
        error = bot.edit_message_text.await_args.kwargs["text"]
        self.assertIn("Custom intervals must be between 10", error)
        self.assertIn("choose the 5-second preset", error)

    async def test_custom_interval_input_is_saved_as_seconds(self):
        ui._ui_drafts[1] = {
            "mode": "edit_interval",
            "config_id": "config-id",
            "interval_seconds": 300,
        }
        ui._custom_interval_inputs[1] = {"chat_id": 1, "message_id": 55}
        message = SimpleNamespace(text="47", chat_id=1)
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=1),
            effective_message=message,
        )
        bot = SimpleNamespace(edit_message_text=AsyncMock())
        with (
            patch.object(ui.autorotate, "edit_config", new=AsyncMock(return_value=True)) as edit,
            patch.object(ui, "_show_configuration", new=AsyncMock()),
        ):
            await ui._handle_custom_interval(update, SimpleNamespace(bot=bot))
        edit.assert_awaited_once_with("config-id", {"interval_seconds": 47})
        self.assertNotIn(1, ui._custom_interval_inputs)

    async def test_configuration_creation_persists_only_the_staged_selection(self):
        config = {"_id": "config-id"}
        draft = {
            "mode": "new",
            "name": "Rotation",
            "group_ids": [101, 202],
            "message_ids": ["one", "two"],
            "interval_seconds": 120,
        }
        with (
            patch.object(ui.autorotate, "create_config", new=AsyncMock(return_value=config)) as create,
            patch.object(ui.autorotate, "edit_config", new=AsyncMock(return_value=True)) as edit,
            patch.object(ui.autorotate, "_is_duplicate", new=AsyncMock(return_value=False)),
        ):
            result = await ui._save_new_config(draft)
        self.assertEqual(result, config)
        create.assert_awaited_once_with("Rotation")
        self.assertEqual(edit.await_args.args[1]["group_ids"], [101, 202])
        self.assertEqual(edit.await_args.args[1]["message_ids"], ["one", "two"])
        self.assertEqual(edit.await_args.args[1]["interval_seconds"], 120)
        self.assertEqual(edit.await_args.args[1]["status"], "stopped")

    async def test_multi_select_callback_flow_saves_and_displays_selected_messages(self):
        groups = [{"chat_id": 101, "title": "First"}, {"chat_id": 202, "title": "Second"}]
        first_id, second_id = ObjectId(), ObjectId()
        messages = [
            {"_id": first_id, "type": "text", "text": "first saved message"},
            {"_id": second_id, "type": "text", "text": "second saved message"},
        ]
        config = {
            "_id": "config-id",
            "name": "Rotation",
            "group_ids": [],
            "message_ids": [],
            "interval_seconds": 300,
            "status": "stopped",
        }
        db = SimpleNamespace(
            groups=_MemoryCollection(groups),
            messages=_MemoryCollection(messages),
        )

        async def edit_config(config_id, updates, reset_rotation=False):
            config.update(updates)
            return True

        query = SimpleNamespace(
            id="flow-0",
            data="ar:create",
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
            from_user=SimpleNamespace(id=1),
        )
        update = SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(id=1),
        )
        context = SimpleNamespace(bot=object())

        async def click(data, callback_id):
            query.data = data
            query.id = callback_id
            await ui.callback_autorotate(update, context)

        with (
            patch.object(ui, "_load_pickable_groups", new=AsyncMock(return_value=(groups, set()))),
            patch.object(ui, "_load_pickable_messages", new=AsyncMock(return_value=messages)),
            patch.object(ui, "get_db", return_value=db),
            patch.object(ui.autorotate, "create_config", new=AsyncMock(return_value=config)),
            patch.object(ui.autorotate, "edit_config", new=AsyncMock(side_effect=edit_config)) as edit,
            patch.object(ui.autorotate, "get_config", new=AsyncMock(return_value=config)),
            patch.object(ui.autorotate, "_is_duplicate", new=AsyncMock(return_value=False)),
        ):
            await click("ar:create", "flow-0")
            await click("ar:gt:101:0", "flow-1")
            await click("ar:gt:202:0", "flow-2")
            await click("ar:gn:0", "flow-3")
            await click(f"ar:mt:{second_id}:0", "flow-4")
            await click(f"ar:mt:{first_id}:0", "flow-5")
            await click("ar:mn:0", "flow-6")
            await click("ar:is:60", "flow-7")
            await click("ar:ok:config-id", "flow-8")

        self.assertEqual(config["group_ids"], [101, 202])
        self.assertEqual(config["message_ids"], [second_id, first_id])
        self.assertEqual(
            edit.await_args_list[-2].args[1]["message_ids"],
            [second_id, first_id],
        )
        rendered = query.edit_message_text.await_args.kwargs["text"]
        self.assertIn("Configuration saved.", rendered)
        self.assertLess(rendered.index("second saved message"), rendered.index("first saved message"))

    async def test_configuration_edits_persist_groups_and_messages(self):
        ui._ui_drafts[1] = {
            "mode": "edit_groups",
            "config_id": "config-id",
            "group_ids": [202],
            "message_ids": ["one"],
        }
        query = SimpleNamespace(edit_message_text=AsyncMock())
        with (
            patch.object(ui.autorotate, "edit_config", new=AsyncMock(return_value=True)) as edit,
            patch.object(ui, "_show_configuration", new=AsyncMock()),
        ):
            await ui._save_group_edit(query, 1)
        self.assertEqual(edit.await_args.args[1], {"group_ids": [202]})
        self.assertNotIn(1, ui._ui_drafts)

        ui._ui_drafts[1] = {
            "mode": "edit_messages",
            "config_id": "config-id",
            "group_ids": [202],
            "message_ids": ["two", "one"],
        }
        with (
            patch.object(ui.autorotate, "edit_config", new=AsyncMock(return_value=True)) as edit,
            patch.object(ui, "_show_configuration", new=AsyncMock()),
        ):
            await ui._save_message_edit(query, 1)
        self.assertEqual(edit.await_args.args[1], {"message_ids": ["two", "one"]})
        self.assertTrue(edit.await_args.kwargs["reset_rotation"])
        self.assertNotIn(1, ui._ui_drafts)

    async def test_callback_query_expiration_prevents_action_execution(self):
        query = SimpleNamespace(
            id="expired",
            data="ar:create",
            answer=AsyncMock(side_effect=ui.BadRequest("Query is too old")),
        )
        update = SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(id=1),
        )
        await ui.callback_autorotate(update, SimpleNamespace())
        self.assertNotIn(1, ui._ui_drafts)


if __name__ == "__main__":
    unittest.main()
