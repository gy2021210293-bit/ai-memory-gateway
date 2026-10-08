import ast
import unittest
from pathlib import Path


class GatewayAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = (Path(__file__).parents[1] / "main.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        helper = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_get_provided_gateway_key"
        )
        namespace = {}
        exec(compile(ast.Module(body=[helper], type_ignores=[]), "main.py", "exec"), namespace)
        cls.get_key = staticmethod(namespace["_get_provided_gateway_key"])

    def test_accepts_openai_bearer_header(self):
        self.assertEqual(
            self.get_key({"Authorization": "Bearer gateway-secret"}, {}),
            "gateway-secret",
        )

    def test_bearer_scheme_is_case_insensitive_and_token_is_trimmed(self):
        self.assertEqual(
            self.get_key({"Authorization": "bearer   gateway-secret  "}, {}),
            "gateway-secret",
        )

    def test_preserves_existing_custom_header_and_query_parameter(self):
        self.assertEqual(
            self.get_key({"X-Gateway-Key": "header-secret"}, {}),
            "header-secret",
        )
        self.assertEqual(
            self.get_key({}, {"gateway_key": "query-secret"}),
            "query-secret",
        )

    def test_rejects_non_bearer_authorization(self):
        self.assertEqual(
            self.get_key({"Authorization": "Basic gateway-secret"}, {}),
            "",
        )

    def test_existing_explicit_key_keeps_precedence(self):
        self.assertEqual(
            self.get_key(
                {
                    "X-Gateway-Key": "header-secret",
                    "Authorization": "Bearer bearer-secret",
                },
                {"gateway_key": "query-secret"},
            ),
            "header-secret",
        )


if __name__ == "__main__":
    unittest.main()
