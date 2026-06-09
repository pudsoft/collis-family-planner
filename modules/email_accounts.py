"""Encrypted storage for per-user email account credentials."""
from cryptography.fernet import Fernet


def _fernet(db):
    row = db.execute("SELECT value FROM app_settings WHERE key='email_enc_key'").fetchone()
    if row:
        key = row[0].encode()
    else:
        key = Fernet.generate_key()
        db.execute(
            "INSERT INTO app_settings (`key`, value) VALUES ('email_enc_key', ?)",
            (key.decode(),),
        )
        db.commit()
    return Fernet(key)


def list_accounts(db, person):
    rows = db.execute(
        "SELECT id, label, email_address FROM email_accounts WHERE person=? ORDER BY id",
        (person,),
    ).fetchall()
    return [{"id": r[0], "label": r[1], "email": r[2]} for r in rows]


def add_account(db, person, label, email_address, app_password):
    token = _fernet(db).encrypt(app_password.encode()).decode()
    db.execute(
        "INSERT INTO email_accounts (person, label, email_address, app_password) VALUES (?,?,?,?)",
        (person, label, email_address, token),
    )
    db.commit()


def remove_account(db, account_id, person):
    db.execute(
        "DELETE FROM email_accounts WHERE id=? AND person=?",
        (account_id, person),
    )
    db.commit()


def get_credentials(db, account_id, person):
    """Return {email, password} for account_id owned by person, or None."""
    row = db.execute(
        "SELECT email_address, app_password FROM email_accounts WHERE id=? AND person=?",
        (account_id, person),
    ).fetchone()
    if not row:
        return None
    password = _fernet(db).decrypt(row[1].encode()).decode()
    return {"email": row[0], "password": password}
