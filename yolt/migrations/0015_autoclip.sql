-- Auto-clip: a subscription with it on queues a run (with the default instructions) for each video its
-- checks find from the moment it was turned on; NULL is off. A found video is auto-clipped at most once
-- (autoclipped_at), and not at all if it has a run already, so a run that failed or was cancelled, or a
-- video clipped by hand, isn't queued again by itself.
ALTER TABLE subscriptions ADD COLUMN autoclip_since timestamptz;
ALTER TABLE channel_videos ADD COLUMN autoclipped_at timestamptz;
