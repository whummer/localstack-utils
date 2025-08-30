import requests
from localstack.utils.strings import short_uid


def test_connect_to_db():
    host = "typedb.localhost.localstack.cloud:4566"

    # get auth token
    response = requests.post(
        f"http://{host}/v1/signin", json={"username": "admin", "password": "password"}
    )
    assert response.ok
    token = response.json()["token"]

    # create database
    db_name = f"db{short_uid()}"
    response = requests.post(
        f"http://{host}/v1/databases/{db_name}",
        json={},
        headers={"Authorization": f"bearer {token}"},
    )
    assert response.ok

    # list databases
    response = requests.get(
        f"http://{host}/v1/databases", headers={"Authorization": f"bearer {token}"}
    )
    assert response.ok
    databases = [db["name"] for db in response.json()["databases"]]
    assert db_name in databases

    # clean up
    response = requests.delete(
        f"http://{host}/v1/databases/{db_name}",
        headers={"Authorization": f"bearer {token}"},
    )
    assert response.ok
