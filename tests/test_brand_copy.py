from app.core.config import Settings


def test_email_sender_uses_the_full_brand_name() -> None:
    assert Settings.model_fields["email_from_name"].default == "Durza Coupons"
    assert Settings.model_fields["email_from"].default == "alerts@durza.in"
