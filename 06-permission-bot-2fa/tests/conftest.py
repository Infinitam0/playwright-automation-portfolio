"""
Test configuration and shared fixtures.

CRITICAL: Environment variables MUST be set BEFORE importing the script module.
The script executes get_config() at module level and will sys.exit(1) if
required variables (PORTAL_USER, PORTAL_PASS, PORTAL_TOTP_SECRET) are missing.
"""

import os
import sys
from pathlib import Path

# Set required env vars BEFORE any test import triggers script module loading.
# JBSWY3DPEHPK3PXP is the public RFC/authenticator-app example secret.
os.environ["PORTAL_USER"] = "test_user"
os.environ["PORTAL_PASS"] = "test_password"
os.environ["PORTAL_TOTP_SECRET"] = "JBSWY3DPEHPK3PXP"
os.environ["HEADLESS"] = "true"

# Add the project root to Python path so we can `import permission_bot`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from openpyxl import Workbook  # noqa: E402

# =============================================================================
# Excel file fixtures
# =============================================================================

@pytest.fixture
def valid_excel(tmp_path):
    """Create a valid permissions Excel file with multiple accounts."""
    filepath = tmp_path / "permissions.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.append(["AccountName", "ParentPath", "CheckboxName", "Action"])
    ws.append([
        "Doe - O'Neil, J.A.",
        "Departments > Unit A > Team 1",
        "Shared folder",
        "check",
    ])
    ws.append([
        "Doe - O'Neil, J.A.",
        "Departments > Unit A",
        "Archive",
        "uncheck",
    ])
    ws.append(["Roe, R.K.", None, "RootPermission", "check"])
    wb.save(filepath)
    return str(filepath)


@pytest.fixture
def excel_with_empty_rows(tmp_path):
    """Create Excel file with empty and whitespace-only rows."""
    filepath = tmp_path / "permissions.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.append(["AccountName", "ParentPath", "CheckboxName", "Action"])
    ws.append(["User A", "Path > Node", "Checkbox1", "check"])
    ws.append([None, None, None, None])
    ws.append(["", "", "", ""])
    ws.append(["User B", "", "Checkbox2", "uncheck"])
    wb.save(filepath)
    return str(filepath)


@pytest.fixture
def excel_bad_headers(tmp_path):
    """Create Excel file with wrong column headers."""
    filepath = tmp_path / "permissions.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.append(["Wrong", "Headers", "Here", "Bad"])
    ws.append(["data", "data", "data", "check"])
    wb.save(filepath)
    return str(filepath)


@pytest.fixture
def excel_bad_action(tmp_path):
    """Create Excel file with invalid action values."""
    filepath = tmp_path / "permissions.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.append(["AccountName", "ParentPath", "CheckboxName", "Action"])
    ws.append(["User A", "Path", "Checkbox1", "invalid_action"])
    ws.append(["User A", "Path", "Checkbox2", "DELETE"])
    ws.append(["User A", "Path", "Checkbox3", "check"])  # only valid row
    wb.save(filepath)
    return str(filepath)


@pytest.fixture
def excel_missing_fields(tmp_path):
    """Create Excel file with missing required fields in various rows."""
    filepath = tmp_path / "permissions.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.append(["AccountName", "ParentPath", "CheckboxName", "Action"])
    ws.append([None, "Path", "Checkbox1", "check"])       # missing AccountName
    ws.append(["User A", "Path", None, "check"])           # missing CheckboxName
    ws.append(["User A", "Path", "Checkbox2", None])       # missing Action
    ws.append(["User A", "Path", "Checkbox3", "check"])    # valid
    wb.save(filepath)
    return str(filepath)


@pytest.fixture
def excel_empty_data(tmp_path):
    """Create Excel file with headers only, no data rows."""
    filepath = tmp_path / "permissions.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.append(["AccountName", "ParentPath", "CheckboxName", "Action"])
    wb.save(filepath)
    return str(filepath)
