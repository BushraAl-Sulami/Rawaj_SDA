from fastapi.testclient import TestClient
from api.main import app

client = TestClient(app)
for path in ['/api/restaurants', '/api/restaurants/2/context']:
    try:
        response = client.get(path)
        print('PATH', path, 'STATUS', response.status_code)
        print(response.text)
    except Exception:
        import traceback
        traceback.print_exc()
