TypeDB on LocalStack
===============================

This repo contains a [LocalStack Extension](https://github.com/localstack/localstack-extensions) that facilitates developing TypeDB-based applications locally.

## Prerequisites

* Docker
* LocalStack Pro (free trial available)
* `localstack` CLI
* `make`

## Install from GitHub repository

This extension can be installed directly from this Github repo via:

```bash
localstack extensions install "git+https://github.com/whummer/localstack-utils.git#egg=localstack-typedb&subdirectory=localstack-typedb"
```

## Install local development version

To install the extension into localstack in developer mode, you will need Python 3.11, and create a virtual environment in the extensions project.

In the newly generated project, simply run

```bash
make install
```

Then, to enable the extension for LocalStack, run

```bash
localstack extensions dev enable .
```

You can then start LocalStack with `EXTENSION_DEV_MODE=1` to load all enabled extensions:

```bash
EXTENSION_DEV_MODE=1 localstack start
```
