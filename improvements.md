## List of jobs
Consider listing the jobs in case the user accidentally refreshes the page

## Cleaning
Is there any left over files that would fill the disks?

## Real time progress

This implementation already provides "near real-time" progress via client-side polling:

- JS submits a job, then polls `/api/jobs/{id}` every ~800ms to display stage + per-track progress.

If you want smoother real-time updates, two good next steps:

1. SSE (Server-Sent Events)
   - Add `GET /api/jobs/{id}/events` that yields `text/event-stream`.
   - Push events from the job runner into a per-job `queue.Queue()` and stream them.
   - Pros: very simple browser client (`EventSource`), no websocket infrastructure.

2. WebSockets
   - Use FastAPI's websocket support to push events.
   - Pros: bi-directional; can add cancel/pause controls.
   - Cons: slightly more moving pieces (connection lifecycle, reconnects).

More granular FFmpeg progress:

- Run ffmpeg with `-progress pipe:1 -nostats` and parse key/value pairs from stdout.
- Update the job's `progress` as `out_time_ms / (track_duration_ms)` for the active track.

