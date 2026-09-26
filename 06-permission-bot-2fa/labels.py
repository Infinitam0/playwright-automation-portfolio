"""
Portal UI labels and selectors used by permission_bot.py and export_permissions.py.

Placeholders; adapt to the target portal. Button names are accessible names and
match as substrings, so an icon prefix in the real label does not matter.
"""

# Login form
LOGIN_USERNAME = "#username"
LOGIN_PASSWORD = "#password"
LOGIN_SUBMIT = "#login"
LOGIN_OTP_CODE = "#code"

# Dashboard link that opens the admin area in a popup (export_permissions.py)
ADMIN_LINK = "a[data-nav='admin']"

# Admin navigation
BTN_USERS = "Users"
BTN_USER_MANAGEMENT = "User management"

# Per-user access editor
TAB_PERMISSIONS = "Permissions"
BTN_MANAGE_ACCESS = "Edit permissions"
BTN_SAVE = "Save"
BTN_OK = "Ok"
BTN_CLOSE = "Close"
DIALOG_CLOSE_X = "button.close, .modal-header button[aria-label='Close'], [data-dismiss='modal']"

# User-tree node element, and the format of its title attribute
# (the original portal padded titles with a trailing space)
TREE_NODE = "div[data-tree-node]"
TREE_NODE_TITLE = "{name} "
