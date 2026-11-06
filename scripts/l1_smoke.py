"""Minimal L1 smoke: register/login/create character/SSE through 5191 -> 8031 -> 9110."""
import json
import sys
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else 'http://127.0.0.1:5191'
USER = 'l1_smoke_20261106'
PASSWORD = 'L1Smoke!2026'


def call(path, payload, token=None):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode(), method='POST')
    req.add_header('Content-Type', 'application/json')
    if token:
        req.add_header('Authorization', 'Bearer ' + token)
    with urllib.request.urlopen(req, timeout=20) as response:
        return response.status, json.loads(response.read().decode())


def main():
    status, _ = call('/api/v1/auth/register', {
        'username': USER,
        'email': USER + '@example.com',
        'password': PASSWORD,
        'full_name': 'L1 Smoke',
    })
    print('REGISTER_STATUS=%d' % status)
    status, login = call('/api/v1/auth/login', {'username': USER, 'password': PASSWORD})
    token = login['data']['access_token']
    print('LOGIN_STATUS=%d' % status)
    status, character = call('/api/v1/characters/', {
        'name': 'L1 Smoke Character',
        'system_prompt': 'L1 smoke only',
    }, token)
    character_id = character['data']['id']
    print('CHARACTER_STATUS=%d' % status)

    req = urllib.request.Request(
        BASE + '/api/v1/characters/%s/speak/stream' % character_id,
        data=json.dumps({'message': 'L1 smoke streaming'}).encode(),
        method='POST',
    )
    req.add_header('Content-Type', 'application/json')
    req.add_header('Authorization', 'Bearer ' + token)
    with urllib.request.urlopen(req, timeout=30) as response:
        body = response.read().decode('utf-8')
    if 'L1-FAKE' not in body or '"type": "complete"' not in body:
        raise RuntimeError('L1 SSE did not return expected fake completion')
    print('SSE_STATUS=200')
    print('SSE_HAS_COMPLETE=True')


if __name__ == '__main__':
    main()
