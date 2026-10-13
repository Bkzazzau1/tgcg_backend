# Meetings and calls

Meetings are calls for the people of an area, or for those invited. The audio
and video run on [LiveKit](https://livekit.com) (LiveKit Cloud to start). This
server never carries the call: it decides who may see and join each meeting,
hands out short-lived join passes, asks LiveKit to close a meeting's room when
it ends, and keeps the record in the audit log.

## Who sees, calls, joins and ends

- **Calling** a meeting needs `startMeeting` for its area, which is the
  caller's own area unless they choose one inside it. It can start now or up
  to 90 days ahead, last 5 to 480 minutes, be sound only or video, and be for
  **everyone in the area** or **only those invited** (access IDs, inside or
  outside the area, at most 200).
- **Seeing and joining**: the host; anyone invited; and, for area meetings,
  everyone holding `joinMeeting` whose own area lies inside the meeting's
  area. A ward meeting reaches the ward's coordinator and agents; a national
  officer does not see every ward's meetings, only national ones, unless
  invited. Agents and observers join once their identity is verified, like
  everything else they do.
- **When**: joining opens 15 minutes before the start and closes 2 hours
  after the planned end, unless the meeting is ended or cancelled first.
- **Ending and cancelling**: the host, or someone holding `startMeeting` over
  the whole area. Ending (once the meeting has opened) and cancelling (any
  time before it ends; a reason is required) close the LiveKit room, which
  disconnects everyone in it. Both are recorded in the audit log, as is
  calling a meeting.

## The join pass

`POST /api/v1/meetings/<reference>/join/` returns the LiveKit URL and a pass
(a JWT signed with the LiveKit API secret, as LiveKit's own SDKs make them)
for the meeting's room, `tgcg-<uuid>`. The pass:

- names the person by access ID, with their name for others to see;
- lets them listen, speak and send data, and show a camera only in a video
  meeting (`canPublishSources`);
- must be used within 30 minutes; once connected, LiveKit renews it.

Each pass handed out is recorded (`Admission`: first and last time, how many
passes). That is who was let in, not how long they stayed.

## Setting up LiveKit

1. Create a project at <https://cloud.livekit.io>. The free plan includes
   5,000 participant-minutes and 50 GB of data a month; video uses far more
   data than sound.
2. In the project's settings, create an API key. Set on the server:

   ```bash
   LIVEKIT_URL=wss://<project>.livekit.cloud
   LIVEKIT_API_KEY=<key>
   LIVEKIT_API_SECRET=<secret>
   ```

   Keep the secret out of the repository and chats, like other keys.
3. Restart the API. Until all three are set, meetings can be planned but
   `join` answers 503 "Calls are not set up on the server yet." and the app
   says so.

LiveKit is open source: the same settings can later point at a LiveKit server
of our own, with no change to the app.

## API

| Endpoint | |
|---|---|
| `GET /meetings/` | Meetings the caller may see, from `since` (default: the last day) on, with `status` (scheduled, live, ended, cancelled), `can_join`, `can_manage` and `calls_available`. |
| `POST /meetings/` | Call a meeting: `title`, `agenda`, `kind` (audio, video), `audience` (area, invited), `starts_at` (default now), `duration_minutes`, `scope`, `invitees`. |
| `GET /meetings/<reference>/` | One meeting. |
| `POST /meetings/<reference>/join/` | The join pass (`url`, `token`, `room`, `expires_at`). |
| `POST /meetings/<reference>/end/` | End it for everyone. |
| `POST /meetings/<reference>/cancel/` | Call it off (`reason`). |

## Not done yet

- Attendance from LiveKit (who stayed, and for how long) through its
  webhooks.
- Recording, screen sharing and dial-in by phone.
