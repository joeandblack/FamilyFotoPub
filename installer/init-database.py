"""Bootstrap a dedicated, non-superuser application role and its database."""
import json
from pathlib import Path
import sys
import psycopg
from psycopg import sql

root=Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from app.backup.admission import hold_process_lease
hold_process_lease(root / 'data')
c=json.loads((root/'config/local.json').read_text())
db=c['database']
with psycopg.connect(host=db['host'],port=db['port'],dbname='postgres',user='postgres',password=c['postgres_admin_password'],autocommit=True) as connection:
    connection.execute("ALTER SYSTEM SET listen_addresses TO '127.0.0.1'")
    connection.execute("ALTER SYSTEM SET password_encryption TO 'scram-sha-256'")
    if not connection.execute('SELECT 1 FROM pg_roles WHERE rolname=%s',(db['user'],)).fetchone():
        connection.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {}').format(sql.Identifier(db['user']),sql.Literal(db['password'])))
    if not connection.execute('SELECT 1 FROM pg_database WHERE datname=%s',(db['dbname'],)).fetchone():
        connection.execute(sql.SQL('CREATE DATABASE {} OWNER {}').format(sql.Identifier(db['dbname']),sql.Identifier(db['user'])))
print('Application role and database ready. Restart the service to apply loopback-only listening. Credentials were not printed.')
