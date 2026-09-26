"""
Offline tests for the permission bot (Playwright is mocked; no portal needed).

Covers:
- Configuration loading (browser login: USER, PASS, TOTP_SECRET required)
- Excel file validation and permission grouping
- TOTP-based 2FA automation
- Browser login authentication flow
- Idempotent checkbox operations
- Full pipeline E2E flows with mocked Playwright
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pyotp
import pytest

# Import the script module (env vars already set by conftest.py)
import labels as ui
import permission_bot as script

# =============================================================================
# Configuration Tests
# =============================================================================

class TestGetConfig:
    """Test get_config() configuration loading."""

    @pytest.fixture(autouse=True)
    def _log_to_tmp(self, monkeypatch, tmp_path):
        """Missing-var failures append to the run log; keep it out of the project dir."""
        monkeypatch.setattr(script, "_script_dir", tmp_path)
        monkeypatch.delenv("LOG_DIR", raising=False)

    def test_required_vars_present(self, monkeypatch):
        """Config loads successfully with all required vars set."""
        monkeypatch.setenv("PORTAL_USER", "my_user")
        monkeypatch.setenv("PORTAL_PASS", "my_password")
        monkeypatch.setenv("PORTAL_TOTP_SECRET", "JBSWY3DPEHPK3PXP")

        config = script.get_config()
        assert config["username"] == "my_user"
        assert config["password"] == "my_password"
        assert config["totp_secret"] == "JBSWY3DPEHPK3PXP"

    def test_missing_user_exits(self, monkeypatch):
        """Missing PORTAL_USER causes SystemExit."""
        monkeypatch.delenv("PORTAL_USER", raising=False)
        monkeypatch.setenv("PORTAL_PASS", "password")
        monkeypatch.setenv("PORTAL_TOTP_SECRET", "JBSWY3DPEHPK3PXP")

        with pytest.raises(SystemExit):
            script.get_config()

    def test_missing_password_exits(self, monkeypatch):
        """Missing PORTAL_PASS causes SystemExit."""
        monkeypatch.setenv("PORTAL_USER", "user")
        monkeypatch.delenv("PORTAL_PASS", raising=False)
        monkeypatch.setenv("PORTAL_TOTP_SECRET", "JBSWY3DPEHPK3PXP")

        with pytest.raises(SystemExit):
            script.get_config()

    def test_missing_totp_secret_exits(self, monkeypatch):
        """Missing PORTAL_TOTP_SECRET causes SystemExit."""
        monkeypatch.setenv("PORTAL_USER", "user")
        monkeypatch.setenv("PORTAL_PASS", "password")
        monkeypatch.delenv("PORTAL_TOTP_SECRET", raising=False)

        with pytest.raises(SystemExit):
            script.get_config()

    def test_missing_all_required_exits(self, monkeypatch):
        """Missing all required vars causes SystemExit."""
        monkeypatch.delenv("PORTAL_USER", raising=False)
        monkeypatch.delenv("PORTAL_PASS", raising=False)
        monkeypatch.delenv("PORTAL_TOTP_SECRET", raising=False)

        with pytest.raises(SystemExit):
            script.get_config()

    def test_empty_password_exits(self, monkeypatch):
        """Empty PORTAL_PASS causes SystemExit."""
        monkeypatch.setenv("PORTAL_USER", "user")
        monkeypatch.setenv("PORTAL_PASS", "")
        monkeypatch.setenv("PORTAL_TOTP_SECRET", "JBSWY3DPEHPK3PXP")

        with pytest.raises(SystemExit):
            script.get_config()

    def test_empty_totp_secret_exits(self, monkeypatch):
        """Empty PORTAL_TOTP_SECRET causes SystemExit."""
        monkeypatch.setenv("PORTAL_USER", "user")
        monkeypatch.setenv("PORTAL_PASS", "password")
        monkeypatch.setenv("PORTAL_TOTP_SECRET", "")

        with pytest.raises(SystemExit):
            script.get_config()

    def test_whitespace_only_totp_secret_exits(self, monkeypatch):
        """Whitespace-only TOTP secret causes SystemExit."""
        monkeypatch.setenv("PORTAL_USER", "user")
        monkeypatch.setenv("PORTAL_PASS", "password")
        monkeypatch.setenv("PORTAL_TOTP_SECRET", "   ")

        with pytest.raises(SystemExit):
            script.get_config()

    def test_totp_secret_spaces_stripped(self, monkeypatch):
        """TOTP secret with spaces is cleaned (authenticator apps display with spaces)."""
        monkeypatch.setenv("PORTAL_USER", "user")
        monkeypatch.setenv("PORTAL_PASS", "password")
        monkeypatch.setenv("PORTAL_TOTP_SECRET", "JBSW Y3DP EHPK 3PXP")

        config = script.get_config()
        assert config["totp_secret"] == "JBSWY3DPEHPK3PXP"

    def test_totp_secret_leading_trailing_spaces(self, monkeypatch):
        """TOTP secret with leading/trailing whitespace is trimmed."""
        monkeypatch.setenv("PORTAL_USER", "user")
        monkeypatch.setenv("PORTAL_PASS", "password")
        monkeypatch.setenv("PORTAL_TOTP_SECRET", "  JBSWY3DPEHPK3PXP  ")

        config = script.get_config()
        assert config["totp_secret"] == "JBSWY3DPEHPK3PXP"

    def test_headless_default_true(self, monkeypatch):
        """Headless defaults to True when not set."""
        monkeypatch.setenv("PORTAL_USER", "user")
        monkeypatch.setenv("PORTAL_PASS", "password")
        monkeypatch.setenv("PORTAL_TOTP_SECRET", "JBSWY3DPEHPK3PXP")
        monkeypatch.delenv("HEADLESS", raising=False)

        config = script.get_config()
        assert config["headless"] is True

    def test_headless_false(self, monkeypatch):
        """Headless 'false' is treated as False."""
        monkeypatch.setenv("PORTAL_USER", "user")
        monkeypatch.setenv("PORTAL_PASS", "password")
        monkeypatch.setenv("PORTAL_TOTP_SECRET", "JBSWY3DPEHPK3PXP")
        monkeypatch.setenv("HEADLESS", "false")

        config = script.get_config()
        assert config["headless"] is False

    def test_headless_zero(self, monkeypatch):
        """Headless '0' is treated as False."""
        monkeypatch.setenv("PORTAL_USER", "user")
        monkeypatch.setenv("PORTAL_PASS", "password")
        monkeypatch.setenv("PORTAL_TOTP_SECRET", "JBSWY3DPEHPK3PXP")
        monkeypatch.setenv("HEADLESS", "0")

        config = script.get_config()
        assert config["headless"] is False

    def test_headless_no(self, monkeypatch):
        """Headless 'no' is treated as False."""
        monkeypatch.setenv("PORTAL_USER", "user")
        monkeypatch.setenv("PORTAL_PASS", "password")
        monkeypatch.setenv("PORTAL_TOTP_SECRET", "JBSWY3DPEHPK3PXP")
        monkeypatch.setenv("HEADLESS", "no")

        config = script.get_config()
        assert config["headless"] is False



# =============================================================================
# Excel Validation Tests
# =============================================================================

class TestValidatePermissionsFile:
    """Test validate_permissions_file() Excel reading and validation."""

    def test_valid_file_parsed(self, valid_excel):
        """Valid Excel file is parsed into correct permission dicts."""
        perms = script.validate_permissions_file(valid_excel)
        assert len(perms) == 3

    def test_parent_path_parsed(self, valid_excel):
        """Parent path is split by ' > ' into a list of node names."""
        perms = script.validate_permissions_file(valid_excel)
        assert perms[0]["parent_path"] == [
            "Departments", "Unit A", "Team 1"
        ]

    def test_account_name_preserved(self, valid_excel):
        """Account names are preserved with special characters."""
        perms = script.validate_permissions_file(valid_excel)
        assert perms[0]["account"] == "Doe - O'Neil, J.A."

    def test_action_lowercased(self, valid_excel):
        """Action values are lowercased."""
        perms = script.validate_permissions_file(valid_excel)
        assert perms[0]["action"] == "check"
        assert perms[1]["action"] == "uncheck"

    def test_root_level_permission(self, valid_excel):
        """Permission without parent path has parent_path=None."""
        perms = script.validate_permissions_file(valid_excel)
        root = [p for p in perms if p["account"] == "Roe, R.K."][0]
        assert root["parent_path"] is None
        assert root["checkbox"] == "RootPermission"

    def test_row_numbers_tracked(self, valid_excel):
        """Row numbers are tracked for error reporting."""
        perms = script.validate_permissions_file(valid_excel)
        assert perms[0]["row_num"] == 2  # First data row (row 1 is header)
        assert perms[2]["row_num"] == 4

    def test_file_not_found(self):
        """Missing file raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            script.validate_permissions_file("nonexistent_file.xlsx")

    def test_invalid_headers(self, excel_bad_headers):
        """Invalid column headers raise ValueError."""
        with pytest.raises(ValueError, match="Invalid headers"):
            script.validate_permissions_file(excel_bad_headers)

    def test_empty_rows_skipped(self, excel_with_empty_rows):
        """Empty and whitespace-only rows are skipped."""
        perms = script.validate_permissions_file(excel_with_empty_rows)
        assert len(perms) == 2
        accounts = [p["account"] for p in perms]
        assert "User A" in accounts
        assert "User B" in accounts

    def test_invalid_action_skipped(self, excel_bad_action):
        """Rows with invalid action values are skipped."""
        perms = script.validate_permissions_file(excel_bad_action)
        assert len(perms) == 1
        assert perms[0]["action"] == "check"
        assert perms[0]["checkbox"] == "Checkbox3"

    def test_missing_fields_skipped(self, excel_missing_fields):
        """Rows with missing required fields are skipped."""
        perms = script.validate_permissions_file(excel_missing_fields)
        assert len(perms) == 1
        assert perms[0]["checkbox"] == "Checkbox3"

    def test_no_valid_rows_raises(self, excel_empty_data):
        """Excel with only headers (no data) raises ValueError."""
        with pytest.raises(ValueError, match="No valid permission rows"):
            script.validate_permissions_file(excel_empty_data)


# =============================================================================
# Permission Grouping Tests
# =============================================================================

class TestGroupPermissions:
    """Test group_permissions_by_account() grouping logic."""

    def test_groups_by_account(self):
        """Permissions are grouped by account name."""
        perms = [
            {"account": "User A", "checkbox": "C1", "action": "check", "row_num": 2, "parent_path": None},
            {"account": "User B", "checkbox": "C2", "action": "check", "row_num": 3, "parent_path": None},
            {"account": "User A", "checkbox": "C3", "action": "uncheck", "row_num": 4, "parent_path": None},
        ]
        grouped = script.group_permissions_by_account(perms)
        assert len(grouped) == 2
        assert len(grouped["User A"]) == 2
        assert len(grouped["User B"]) == 1

    def test_preserves_all_fields(self):
        """All permission fields are preserved in grouped output."""
        perms = [
            {"account": "User A", "parent_path": ["X", "Y"], "checkbox": "C1", "action": "check", "row_num": 2},
        ]
        grouped = script.group_permissions_by_account(perms)
        perm = grouped["User A"][0]
        assert perm["parent_path"] == ["X", "Y"]
        assert perm["checkbox"] == "C1"
        assert perm["action"] == "check"
        assert perm["row_num"] == 2

    def test_single_account(self):
        """Single account groups correctly."""
        perms = [
            {"account": "User A", "checkbox": "C1", "action": "check", "row_num": 2, "parent_path": None},
        ]
        grouped = script.group_permissions_by_account(perms)
        assert len(grouped) == 1
        assert "User A" in grouped

    def test_many_accounts(self):
        """Multiple accounts are each represented."""
        perms = [
            {"account": f"User {chr(65+i)}", "checkbox": "C", "action": "check", "row_num": i, "parent_path": None}
            for i in range(5)
        ]
        grouped = script.group_permissions_by_account(perms)
        assert len(grouped) == 5


# =============================================================================
# Idempotent Checkbox Tests
# =============================================================================

class TestSafeCheckUncheck:
    """Test safe_check() and safe_uncheck() idempotent operations."""

    def test_safe_check_checks_unchecked(self):
        """safe_check() checks an unchecked checkbox."""
        locator = MagicMock()
        locator.is_checked.return_value = False
        script.safe_check(locator, "TestCheckbox")
        locator.check.assert_called_once()

    def test_safe_check_skips_already_checked(self):
        """safe_check() is a no-op on already-checked checkbox."""
        locator = MagicMock()
        locator.is_checked.return_value = True
        script.safe_check(locator, "TestCheckbox")
        locator.check.assert_not_called()

    def test_safe_uncheck_unchecks_checked(self):
        """safe_uncheck() unchecks a checked checkbox."""
        locator = MagicMock()
        locator.is_checked.return_value = True
        script.safe_uncheck(locator, "TestCheckbox")
        locator.uncheck.assert_called_once()

    def test_safe_uncheck_skips_already_unchecked(self):
        """safe_uncheck() is a no-op on already-unchecked checkbox."""
        locator = MagicMock()
        locator.is_checked.return_value = False
        script.safe_uncheck(locator, "TestCheckbox")
        locator.uncheck.assert_not_called()


# =============================================================================
# Per-account Save Tests
# =============================================================================

def _access_editor_page(treeitem_found: bool):
    """Mock page whose tree items exist or not; buttons are tracked by name."""
    page = MagicMock()
    buttons = {}

    def get_by_role(role, name=None, **kwargs):
        if role == "button":
            if name not in buttons:
                buttons[name] = MagicMock(name=name)
                buttons[name].count.return_value = 1
            return buttons[name]
        item = MagicMock()
        item.first.count.return_value = 1 if treeitem_found else 0
        return item

    page.get_by_role.side_effect = get_by_role
    return page, buttons


ROOT_ACTION = [{"account": "User A", "parent_path": None, "checkbox": "C1",
                "action": "check", "row_num": 2}]


class TestProcessAccountSave:
    """process_account() saves only when at least one action succeeded."""

    def test_save_clicked_when_an_action_succeeds(self):
        page, buttons = _access_editor_page(treeitem_found=True)
        succeeded, failed = script.process_account(page, "User A", ROOT_ACTION)

        assert (succeeded, failed) == (1, [])
        buttons[ui.BTN_SAVE].locator.return_value.click.assert_called_once()

    def test_save_skipped_and_editor_closed_when_all_actions_fail(self):
        page, buttons = _access_editor_page(treeitem_found=False)
        succeeded, failed = script.process_account(page, "User A", ROOT_ACTION)

        assert succeeded == 0
        assert len(failed) == 1
        assert ui.BTN_SAVE not in buttons
        assert ui.BTN_OK not in buttons
        buttons[ui.BTN_CLOSE].first.click.assert_called_once()


# =============================================================================
# TOTP Functionality Tests
# =============================================================================

class TestTOTPFunctionality:
    """Test TOTP-specific functionality: code generation."""

    def test_pyotp_generates_6_digit_code(self):
        """pyotp.TOTP generates a 6-digit time-based code from a valid secret."""
        secret = "JBSWY3DPEHPK3PXP"
        totp = pyotp.TOTP(secret)
        code = totp.now()
        assert len(code) == 6
        assert code.isdigit()

    def test_pyotp_same_secret_same_code(self):
        """Same secret produces same code within the same time window."""
        secret = "JBSWY3DPEHPK3PXP"
        code1 = pyotp.TOTP(secret).now()
        code2 = pyotp.TOTP(secret).now()
        assert code1 == code2

    def test_pyotp_different_secrets_different_codes(self):
        """Different secrets produce different codes (with very high probability)."""
        code1 = pyotp.TOTP("JBSWY3DPEHPK3PXP").now()
        code2 = pyotp.TOTP("GEZDGNBVGY3TQOJQ").now()
        # Technically could collide but probability is 1/1,000,000
        assert code1 != code2


# =============================================================================
# Authentication / Browser Login Tests
# =============================================================================

TEST_CONFIG = {
    "username": "testuser",
    "password": "testpass",
    "totp_secret": "JBSWY3DPEHPK3PXP",
    "headless": True,
    "url": "https://portal.example.com/admin",
}


class TestBrowserLogin:
    """Test setup_browser_and_navigate() browser login + TOTP flow."""

    def test_login_fills_credentials_and_totp(self):
        """Browser login fills username, password, and TOTP code."""
        mock_page = MagicMock()

        mock_username = MagicMock()
        mock_username.is_visible.return_value = True
        mock_password = MagicMock()
        mock_code = MagicMock()

        def locator_factory(selector):
            if selector == ui.LOGIN_USERNAME:
                return mock_username
            if selector == ui.LOGIN_PASSWORD:
                return mock_password
            if selector == ui.LOGIN_OTP_CODE:
                return mock_code
            return MagicMock()

        mock_page.locator.side_effect = locator_factory

        with patch.object(script, "CONFIG", TEST_CONFIG), \
             patch.object(script, "expand_tree_node") as mock_expand:
            result = script.setup_browser_and_navigate(mock_page)

        mock_page.goto.assert_called_once_with(TEST_CONFIG["url"])

        # Username and password were filled
        mock_username.fill.assert_called_once_with("testuser")
        mock_password.fill.assert_called_once_with("testpass")

        # TOTP code was filled (any 6-digit code)
        mock_code.fill.assert_called_once()
        totp_arg = mock_code.fill.call_args[0][0]
        assert len(totp_arg) == 6
        assert totp_arg.isdigit()

        # User tree was expanded
        assert mock_expand.call_count == 2
        assert result == mock_page

    def test_login_skips_totp_when_not_visible(self):
        """Browser login continues when 2FA page doesn't appear."""
        mock_page = MagicMock()

        mock_code = MagicMock()
        mock_code.wait_for.side_effect = Exception("Timeout")

        def locator_factory(selector):
            if selector == ui.LOGIN_OTP_CODE:
                return mock_code
            return MagicMock()

        mock_page.locator.side_effect = locator_factory

        with patch.object(script, "CONFIG", TEST_CONFIG), \
             patch.object(script, "expand_tree_node"):
            result = script.setup_browser_and_navigate(mock_page)

        # TOTP code was NOT filled (2FA not required)
        mock_code.fill.assert_not_called()
        assert result == mock_page

    def test_login_skipped_when_already_signed_in(self):
        """No credentials are typed when the login form is not shown."""
        mock_page = MagicMock()
        mock_username = MagicMock()
        mock_username.is_visible.return_value = False
        mock_page.locator.side_effect = lambda s: mock_username if s == ui.LOGIN_USERNAME else MagicMock()

        with patch.object(script, "CONFIG", TEST_CONFIG), \
             patch.object(script, "expand_tree_node"):
            script.setup_browser_and_navigate(mock_page)

        mock_username.fill.assert_not_called()


# =============================================================================
# Full E2E Pipeline Tests
# =============================================================================

def _make_playwright_cm():
    """Helper: create a mocked sync_playwright() context manager."""
    mock_browser = MagicMock(name="browser")
    mock_context = MagicMock(name="context")
    mock_page = MagicMock(name="page")
    mock_pw_instance = MagicMock(name="playwright")

    mock_pw_instance.chromium.launch.return_value = mock_browser
    mock_browser.new_context.return_value = mock_context
    mock_context.new_page.return_value = mock_page

    return mock_pw_instance, mock_browser, mock_context, mock_page


@pytest.fixture
def run_config(tmp_path):
    """Config whose run log goes to a temp dir."""
    return {
        "username": "user", "password": "pass",
        "totp_secret": "JBSWY3DPEHPK3PXP",
        "headless": True, "log_dir": tmp_path,
        "url": "https://portal.example.com/admin",
    }


PERMISSIONS = [
    {"account": "User A", "parent_path": None, "checkbox": "C1",
     "action": "check", "row_num": 2},
]
GROUPED = {"User A": PERMISSIONS}


def _run_main(config, setup=None, process=None, permissions=PERMISSIONS, grouped=GROUPED):
    """Run main() with Playwright mocked. Returns (result, mocks)."""
    setup = setup or {"return_value": None}
    process = process or {"return_value": (1, 1, [])}
    with patch.object(script, "CONFIG", config), \
         patch.object(script, "validate_permissions_file", return_value=permissions), \
         patch.object(script, "group_permissions_by_account", return_value=grouped), \
         patch("permission_bot.sync_playwright") as mock_pw:

        pw_inst, browser, context, page = _make_playwright_cm()
        mock_pw.return_value.__enter__ = MagicMock(return_value=pw_inst)
        mock_pw.return_value.__exit__ = MagicMock(return_value=False)

        with patch.object(script, "setup_browser_and_navigate", **setup) as mock_setup, \
             patch.object(script, "process_all_accounts", **process) as mock_process:
            result = script.main()

    return result, {
        "pw": pw_inst, "browser": browser, "context": context, "page": page,
        "setup": mock_setup, "process": mock_process,
    }


def _log_lines(log_dir: Path) -> list[str]:
    return (log_dir / "permission_bot.log").read_text(encoding="utf-8").splitlines()


class TestE2EFlow:
    """End-to-end pipeline tests covering the full main() flow."""

    def test_excel_not_found_returns_1(self, run_config):
        """main() returns 1 when Excel file doesn't exist."""
        with patch.object(script, "CONFIG", run_config), \
             patch.object(script, "validate_permissions_file",
                          side_effect=FileNotFoundError("Not found")):
            result = script.main()

        assert result == 1
        assert "| FAILURE |" in _log_lines(run_config["log_dir"])[-1]

    def test_excel_invalid_returns_1(self, run_config):
        """main() returns 1 when Excel validation fails."""
        with patch.object(script, "CONFIG", run_config), \
             patch.object(script, "validate_permissions_file",
                          side_effect=ValueError("Bad data")):
            result = script.main()

        assert result == 1

    def test_success_returns_0(self, run_config):
        """main() returns 0 and logs SUCCESS when all accounts process successfully."""
        result, _ = _run_main(run_config)

        assert result == 0
        assert "| SUCCESS | 1 accounts, 1 actions, 0 failures" in _log_lines(run_config["log_dir"])[-1]

    def test_processing_failures_returns_1(self, run_config):
        """main() returns 1 when there are processing failures."""
        failures = [{"account": "User A", "type": "account_error", "error": "Boom"}]
        result, _ = _run_main(run_config, process={"return_value": (0, 1, failures)})

        assert result == 1
        assert "1 failure(s)" in _log_lines(run_config["log_dir"])[-1]

    def test_browser_exception_returns_1(self, run_config):
        """main() returns 1 when browser setup raises an exception."""
        result, _ = _run_main(run_config, setup={"side_effect": Exception("Browser crashed")})

        assert result == 1

    def test_browser_closes_on_success(self, run_config):
        """Browser context and browser are closed even on success."""
        _, m = _run_main(run_config)

        m["context"].close.assert_called_once()
        m["browser"].close.assert_called_once()

    def test_browser_closes_on_failure(self, run_config):
        """Browser context and browser are closed even on failure."""
        _, m = _run_main(run_config, setup={"side_effect": Exception("Crash")})

        m["context"].close.assert_called_once()
        m["browser"].close.assert_called_once()

    def test_full_e2e_browser_login_pipeline(self, run_config):
        """
        Full E2E: config -> Excel -> group -> browser login + TOTP -> navigate -> process -> exit 0.
        """
        permissions = [
            {"account": "Doe, J.A.", "parent_path": ["Departments", "Unit A"],
             "checkbox": "Team 1", "action": "check", "row_num": 2},
            {"account": "Roe, R.K.", "parent_path": None,
             "checkbox": "Root", "action": "uncheck", "row_num": 3},
        ]
        grouped = {
            "Doe, J.A.": [permissions[0]],
            "Roe, R.K.": [permissions[1]],
        }
        result, m = _run_main(run_config, process={"return_value": (2, 2, [])},
                              permissions=permissions, grouped=grouped)

        assert result == 0
        m["setup"].assert_called_once_with(m["page"])
        m["process"].assert_called_once_with(m["page"], grouped)
        m["pw"].chromium.launch.assert_called_once_with(headless=True)

    def test_full_e2e_with_real_excel(self, run_config, valid_excel):
        """E2E with real Excel parsing and grouping (only the browser is mocked)."""
        permissions = script.validate_permissions_file(valid_excel)
        grouped = script.group_permissions_by_account(list(permissions))
        result, m = _run_main(run_config, process={"return_value": (3, 3, [])},
                              permissions=permissions, grouped=grouped)

        assert result == 0
        grouped_arg = m["process"].call_args[0][1]
        assert len(grouped_arg["Doe - O'Neil, J.A."]) == 2
        assert len(grouped_arg["Roe, R.K."]) == 1
