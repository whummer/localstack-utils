import requests
from localstack.utils.strings import short_uid
from typedb.driver import TypeDB, Credentials, DriverOptions, TransactionType


def test_connect_to_db_via_http_api():
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


def test_connect_to_db_via_grpc_endpoint():
    # test taken from https://typedb.com/docs/drivers/2.x/python/

    db_name = "access-management-db"
    server_host = "typedb.localhost.localstack.cloud:4566"
    # server_host = "localhost:1729"

    driver_cfg = TypeDB.driver(
        server_host,
        Credentials("admin", "password"),
        DriverOptions(is_tls_enabled=False),
    )
    with driver_cfg as driver:
        if driver.databases.contains(db_name):
            driver.databases.get(db_name).delete()
        driver.databases.create(db_name)
        print("!create driver transaction", driver, type(driver))
        with driver.transaction(db_name, TransactionType.SCHEMA) as tx:
            pass
        #     tx.query("define person sub entity;").resolve()
        #     tx.query(
        #         "define name sub attribute, value string; person owns name;"
        #     ).resolve()
        #     tx.commit()
        #
        # with driver.transaction(db_name, TransactionType.WRITE) as tx:
        #     tx.query("insert $p isa person, has name 'Alice';").resolve()
        #     tx.query("insert $p isa person, has name 'Bob';").resolve()
        #     tx.commit()
        # with driver.transaction(db_name, TransactionType.READ) as tx:
        #     results = tx.query("match $p isa person; fetch $p: name;").resolve()
        #     for json in results:
        #         print(json)
