# Changelog

## 0.9.0

- Publish all 740 readable register definitions from the pinned `huawei-solar` catalog
  for EMMA, SUN2000/LUNA, SmartLogger, SDongle, and SCharger devices.
- Keep newly added catalog entities disabled by default and synchronize the enabled Home
  Assistant entity registry with the connector's active Modbus polling subscription.
- Route enabled registers to their discovered Modbus unit, skip absent optional hardware,
  and wake the scheduler immediately when subscriptions change.
- Classify entities into live sensors, safe configuration controls, and diagnostics;
  normalize units and expand device-specific icons and fast/medium/slow grouping.
- Preserve unknown upstream writeable registers as read-only diagnostics unless an
  explicit safe control schema is defined.

## 0.8.1

- Fix the Home Assistant configuration and options forms by using the serializable
  native port validator instead of a plain Python callback.
- Add AGPL-3.0 licensing and complete the repository metadata required by HACS.
- Correct manifest key ordering for Hassfest validation.

## 0.8.0

- Make the integration self-contained and ready for installation as a HACS custom
  repository.
- Add embedded Home Assistant reverse Modbus/TLS server mode with configurable port,
  managed local CA/certificate generation, and custom certificate/key support.
- Preserve external connector mode and suggest a random `EMMA_API_TOKEN` during setup.
- Migrate existing version-1 config entries to external mode without changing their
  connector credentials.
- Add stable embedded entity IDs and a one-time topology refresh after EMMA connects.
- Add HACS, Hassfest, unit-test, and Dependabot GitHub configuration.
- Add repository maintenance guidance and an ESPHome/LilyGO T-ETH-Elite roadmap.

## 0.7.1

- Add end-to-end debug logging for user controls, service/API validation, connector
  writes, readback, rejection, and completion.
