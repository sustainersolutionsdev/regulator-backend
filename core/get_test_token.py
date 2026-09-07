from dotenv import load_dotenv
load_dotenv()
import requests

API_KEY = "REDACTED"

def get_id_token(email: str, password: str) -> str:
    url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={API_KEY}"
    resp = requests.post(url, json={
        "email": email,
        "password": password,
        "returnSecureToken": True,
    })
    resp.raise_for_status()
    return resp.json()["idToken"]


if __name__ == "__main__":
    token = get_id_token("admin@testtenant.com", "TempPassword123!")
    print(token)