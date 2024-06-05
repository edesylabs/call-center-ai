from aiohttp_retry import ExponentialRetry, RetryClient
from helpers.http import aiohttp_session
from helpers.config_models.sms import TwilioModel
from helpers.logging import logger
from helpers.pydantic_types.phone_numbers import PhoneNumber
from models.readiness import ReadinessStatus
from persistence.isms import ISms
from twilio.base.exceptions import TwilioRestException
from twilio.http.async_http_client import AsyncTwilioHttpClient
from twilio.rest import Client
from typing import Optional


class TwilioSms(ISms):
    _client: Optional[Client] = None
    _config: TwilioModel

    def __init__(self, config: TwilioModel):
        logger.info(f"Using Twilio from number {config.phone_number}")
        self._config = config

    async def areadiness(self) -> ReadinessStatus:
        """
        Check the readiness of the Twilio SMS service.

        This only check if the Twilio API is reachable and the account has remaining balance.
        """
        account_sid = self._config.account_sid
        client = await self._use_client()
        try:
            account = await client.api.accounts(account_sid).fetch_async()
            balance = account.balance.fetch()
            assert balance.balance and float(balance.balance) > 0
            return ReadinessStatus.OK
        except AssertionError:
            logger.error("Readiness test failed", exc_info=True)
        return ReadinessStatus.FAIL

    async def asend(self, content: str, phone_number: PhoneNumber) -> bool:
        logger.info(f"Sending SMS to {phone_number}")
        success = False
        logger.info(f"SMS content: {content}")
        client = await self._use_client()
        try:
            res = await client.messages.create_async(
                body=content,
                from_=str(self._config.phone_number),
                to=phone_number,
            )
            # TODO: How to check the delivery status? Seems present in "res.status" but not documented
            if res.error_message:
                logger.warning(
                    f"Failed SMS to {phone_number}, status {res.error_code}, error {res.error_message}"
                )
            else:
                logger.debug(f"SMS sent to {phone_number}")
                success = True
        except TwilioRestException as e:
            logger.error(f"Error sending SMS: {e}")
        except Exception:
            logger.warning(f"Failed SMS to {phone_number}", exc_info=True)
        return success

    async def _use_client(self) -> Client:
        if not self._client:
            http = AsyncTwilioHttpClient()
            http.session = RetryClient(
                client_session=await aiohttp_session(),
                retry_options=ExponentialRetry(attempts=3),
            )  # Use the same session as the rest of the application, and retry 3 times with exponential backoff with the same lib as Twilio
            self._client = Client(
                # Performance
                http_client=http,
                # Authentication
                password=self._config.auth_token.get_secret_value(),
                username=self._config.account_sid,
            )
        return self._client
