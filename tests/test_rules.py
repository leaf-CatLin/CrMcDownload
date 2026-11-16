import unittest

from offcraft.meta.rules import OsInfo, is_allowed, native_classifier, rule_matches


WIN = OsInfo("windows", "10.0.19045", "x86_64")


class RulesTest(unittest.TestCase):
    def test_no_rules_allows(self) -> None:
        self.assertTrue(is_allowed(None, WIN))
        self.assertTrue(is_allowed([], WIN))

    def test_disallow_other_os(self) -> None:
        rules = [{"action": "allow"}, {"action": "disallow", "os": {"name": "osx"}}]
        self.assertTrue(is_allowed(rules, WIN))
        self.assertFalse(is_allowed(rules, OsInfo("osx", "14.0", "arm64")))

    def test_allow_only_windows(self) -> None:
        rules = [{"action": "allow", "os": {"name": "windows"}}]
        self.assertTrue(is_allowed(rules, WIN))
        self.assertFalse(is_allowed(rules, OsInfo("linux", "6.8", "x86_64")))

    def test_arch(self) -> None:
        rules32 = [{"action": "allow", "os": {"arch": "x86"}}]
        self.assertFalse(is_allowed(rules32, WIN))
        self.assertTrue(is_allowed(rules32, OsInfo("windows", "10.0", "x86")))

    def test_os_version_regex(self) -> None:
        rules = [{"action": "allow", "os": {"name": "windows", "version": "^10\\."}}]
        self.assertTrue(is_allowed(rules, WIN))
        self.assertFalse(is_allowed(rules, OsInfo("windows", "6.1", "x86_64")))

    def test_features(self) -> None:
        rules = [{"action": "allow", "features": {"is_demo_user": True}}]
        self.assertFalse(is_allowed(rules, WIN))
        self.assertTrue(is_allowed(rules, WIN, {"is_demo_user": True}))

    def test_last_matching_rule_wins(self) -> None:
        rules = [
            {"action": "allow"},
            {"action": "disallow", "os": {"name": "windows"}},
        ]
        self.assertFalse(is_allowed(rules, WIN))

    def test_rule_matches_without_conditions(self) -> None:
        self.assertTrue(rule_matches({}, WIN))

    # --- natives 分类器（架构相关） ---
    def test_native_classifier_default_windows(self) -> None:
        lib = {"natives": {"windows": "natives-windows", "linux": "natives-linux"}}
        self.assertEqual(native_classifier(lib, WIN), "natives-windows")

    def test_native_classifier_arm64_prefers_arch_specific(self) -> None:
        lib = {"natives": {"windows": "natives-windows", "windows-arm64": "natives-windows-arm64"}}
        arm = OsInfo("windows", "10.0", "arm64")
        self.assertEqual(native_classifier(lib, arm), "natives-windows-arm64")
        self.assertEqual(native_classifier(lib, WIN), "natives-windows")

    def test_native_classifier_osx(self) -> None:
        lib = {"natives": {"osx": "natives-osx", "osx-arm64": "natives-macos-arm64"}}
        self.assertEqual(native_classifier(lib, OsInfo("osx", "14.0", "x86_64")), "natives-osx")
        self.assertEqual(
            native_classifier(lib, OsInfo("osx", "14.0", "arm64")), "natives-macos-arm64"
        )

    def test_native_classifier_absent_returns_none(self) -> None:
        self.assertIsNone(native_classifier({}, WIN))
        self.assertIsNone(native_classifier({"natives": {"linux": "x"}}, WIN))

    def test_arm64_rule_matches(self) -> None:
        rules = [{"action": "allow", "os": {"name": "osx", "arch": "arm64"}}]
        self.assertTrue(is_allowed(rules, OsInfo("osx", "14.0", "arm64")))
        self.assertFalse(is_allowed(rules, OsInfo("osx", "14.0", "x86_64")))


if __name__ == "__main__":
    unittest.main()
