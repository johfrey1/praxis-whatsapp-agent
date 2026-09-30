from app.whatsapp.parser import parse_failed_statuses, parse_incoming_messages


def _payload(statuses: list[dict]) -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "1", "changes": [{"field": "messages", "value": {"statuses": statuses}}]}],
    }


def test_parse_failed_statuses_extracts_error() -> None:
    payload = _payload(
        [
            {"id": "wamid.ok", "status": "delivered", "recipient_id": "573001112233"},
            {
                "id": "wamid.fail",
                "status": "failed",
                "recipient_id": "573102394548",
                "errors": [
                    {
                        "code": 131047,
                        "title": "Re-engagement message",
                        "error_data": {"details": "Message failed to send because more than 24 hours have passed"},
                    }
                ],
            },
        ]
    )

    failed = parse_failed_statuses(payload)

    assert len(failed) == 1
    assert failed[0].wa_message_id == "wamid.fail"
    assert failed[0].recipient_id == "573102394548"
    assert failed[0].error_code == 131047
    assert "24 hours" in failed[0].error_details
    assert parse_incoming_messages(payload) == []


def test_parse_failed_statuses_tolerates_missing_errors() -> None:
    failed = parse_failed_statuses(_payload([{"id": "x", "status": "failed", "recipient_id": "57300"}]))
    assert failed[0].error_code is None
