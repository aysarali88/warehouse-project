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

        with patch.object(main, "requisition_header_to_dict", return_value={"id": 1}):
            result = main.list_material_requisition_headers(
                limit=80, db=db, viewer="Aysar", role="Admin", program="FTTH"
            )

        query.limit.assert_called_once_with(80)
        self.assertEqual(result["requisitions"], [{"id": 1}])


if __name__ == "__main__":
    unittest.main()
