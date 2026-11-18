"""The call pipeline: verify, authorize, record.

Read `pipeline.py` first - it carries the gate order and the reason each gate sits
where it does. The other modules are the pieces that order composes:

    envelope.py     signed-envelope verification
    nonce_ledger.py gate 8, envelope replay
    authorize.py    tenancy + grant lookup
    capability.py   the deploy-dark biscuit seam - raises, deliberately
    repository.py   the SQL, against columns read off the live schema
    errors.py       the wire error codes and HTTP statuses
"""
