from dotenv import load_dotenv
load_dotenv()

from audit import write_with_audit
from google.cloud import firestore

db = firestore.Client(project='regulator-dev')
bad_ref = db.collection('tenants').document('test-tenant-001').collection('users').document('nonexistent-uid')

try:
    write_with_audit(
        tenant_id='test-tenant-001',
        doc_ref=bad_ref,
        data={'hidden': True, 'badField': object()},
        actor_uid='m9PW1W0tmNcl93fOEKjPIHPEU2F3',
        actor_role='admin',
        action='forced_failure_test',
    )
except Exception as e:
    print('Expected failure:', type(e).__name__, e)