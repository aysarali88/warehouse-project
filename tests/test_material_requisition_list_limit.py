from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import main


class MaterialRequisitionListLimitTests(unittest.TestCase):
    def test_admin_list_limits_rows_in_database_query(self):
        query = MagicMock()
        query.options.return_value = query
        query.filter.return_value = query
        query.order_by.return_value = query
        query.limit.return_value = query
        query.all.return_value = [SimpleNamespace(id=1)]
        db = SimpleNamespace(query=MagicMock(return_value=query))
        request = SimpleNamespace()

        with (
            patch.object(main, "requisition_view_context", return_value=("Aysar", "Admin", "", False)),
            patch.object(main, "requisition_to_dict", return_value={"id": 1}),
        ):
            result = main.list_material_requisitions(request, limit=200, program="FTTH", db=db)

        query.limit.assert_called_once_with(200)
        self.assertEqual(result["requisitions"], [{"id": 1}])

    def test_admin_list_caps_requested_limit_at_two_hundred(self):
        query = MagicMock()
        query.options.return_value = query
        query.filter.return_value = query
        query.order_by.return_value = query
        query.limit.return_value = query
        query.all.return_value = []
        db = SimpleNamespace(query=MagicMock(return_value=query))

        with patch.object(main, "requisition_view_context", return_value=("Aysar", "Admin", "", False)):
            main.list_material_requisitions(SimpleNamespace(), limit=5000, program="FTTH", db=db)

        query.limit.assert_called_once_with(200)

    def test_admin_bootstrap_headers_limit_rows_in_database_query(self):
        query = MagicMock()
        query.options.return_value = query
        query.filter.return_value = query
        query.order_by.return_value = query
        query.limit.return_value = query
        query.all.return_value = [SimpleNamespace(id=1)]
        db = SimpleNamespace(query=MagicMock(return_value=query))
        diagnostics = {}

        with patch.object(main, "requisition_header_to_dict", return_value={"id": 1}):
            result = main.list_material_requisition_headers(
                limit=80, db=db, viewer="Aysar", role="Admin", program="FTTH", diagnostics=diagnostics
            )

        query.limit.assert_called_once_with(80)
        self.assertEqual(result["requisitions"], [{"id": 1}])
        self.assertEqual(diagnostics["mr_rows_loaded"], 1)
        self.assertEqual(diagnostics["mr_rows_returned"], 1)

    def test_reserved_quantities_read_only_required_item_columns(self):
        requisition_query = MagicMock()
        requisition_query.join.return_value = requisition_query
        requisition_query.filter.return_value = requisition_query
        requisition_query.union_all.return_value = requisition_query
        requisition_query.all.return_value = [
            (10, 2, 3.5), (10, 2, 1.0), (11, None, 4.0),
            (10, 3, 2.0), (12, 2, 4.0),
        ]

        transfer_query = MagicMock()
        transfer_query.join.return_value = transfer_query
        transfer_query.filter.return_value = transfer_query
        transfer_query.all.return_value = [(10, 3, 2.0), (12, 2, 4.0)]

        db = SimpleNamespace(query=MagicMock(side_effect=[requisition_query, transfer_query]))
        result = main.reserved_stock_quantities(db, "FTTH")

        self.assertEqual(result, {(10, 2): 4.5, (10, 3): 2.0, (12, 2): 4.0})
        self.assertEqual(db.query.call_count, 2)
        requisition_query.union_all.assert_called_once_with(transfer_query)


class VirtualDamageReportTests(unittest.TestCase):
    def test_damage_events_use_one_joined_query_not_one_query_per_event(self):
        warehouse_query = MagicMock()
        warehouse_query.filter.return_value = warehouse_query
        warehouse_query.all.return_value = [SimpleNamespace(id=4)]

        balance_query = MagicMock()
        balance_query.options.return_value = balance_query
        balance_query.filter.return_value = balance_query
        balance_query.all.return_value = []

        movement_query = MagicMock()
        movement_query.outerjoin.return_value = movement_query
        movement_query.options.return_value = movement_query
        movement_query.filter.return_value = movement_query
        movement_query.order_by.return_value = movement_query
        movement_query.limit.return_value = movement_query
        movement_query.all.return_value = []

        db = SimpleNamespace(query=MagicMock(side_effect=[warehouse_query, balance_query, movement_query]))
        request = SimpleNamespace()

        with patch.object(main, "current_user", return_value=SimpleNamespace(role="Admin")):
            result = main.virtual_damage_report(request, db, "FTTH")

        self.assertEqual(db.query.call_count, 3)
        self.assertEqual(result, {"total_quantity": 0, "items": [], "events": []})


if __name__ == "__main__":
    unittest.main()
