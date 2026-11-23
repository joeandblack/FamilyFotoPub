"""Active import duration reconstructed from durable state transitions."""
def active_seconds(started, events, now):
    active=True
    previous=started
    seconds=0.0
    for event in events:
        at=min(now,max(previous,event['created_at']))
        if active:
            seconds+=(at-previous).total_seconds()
        previous=at
        if event['event'] in ('running','scanning'):
            active=True
        elif event['event'] in ('paused','paused-error','completed-review-required','cancelled'):
            active=False
    if active:
        seconds+=max(0,(now-previous).total_seconds())
    return max(0,seconds)
