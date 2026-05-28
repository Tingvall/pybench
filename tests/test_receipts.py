from pybench.receipts import error_receipt, success_receipt


def test_success_receipt_shape():
    r = success_receipt(outputs=[{"artifact_id": "a1", "type": "relation_family"}])
    assert r["status"] == "ok"
    assert isinstance(r["outputs"], list)


def test_error_receipt_shape():
    r = error_receipt("boom")
    assert r["status"] == "error"
    assert r["message"] == "boom"
