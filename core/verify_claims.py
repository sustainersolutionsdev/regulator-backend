import firebase_admin
from firebase_admin import credentials, auth
import os

cred = credentials.Certificate(os.environ["GOOGLE_APPLICATION_CREDENTIALS"])
firebase_admin.initialize_app(cred)

uid = "m9PW1W0tmNcl93fOEKjPIHPEU2F3"  # from your create_user output
user = auth.get_user(uid)
print("Custom claims:", user.custom_claims)