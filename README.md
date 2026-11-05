# CryoHealth-geo

Python FastAPI geospatial service: Sentinel-2 lake monitoring, NDWI water extent, hazard index. See the CryoHealth PRD in uExel/cryo-harness.

For event-driven processing, subscribe the configured AWS SNS topic to
`https://<geo-host>/webhooks/stac-event` and set `AWS_SNS_TOPIC_ARN`. The endpoint
verifies SNS signatures, confirms subscriptions, then queues the existing observation
and hazard pass. Configure the topic/filter policy to publish only relevant Sentinel
acquisition events; live delivery also requires a reachable public HTTPS endpoint.
