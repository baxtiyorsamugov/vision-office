# Vision Office UI

The approved dashboard reference uses a pale gray workspace, white surfaces,
green primary actions, soft secondary tones and rounded navigation. The application
adapts this language to camera operations and employee attendance.

## Shared Styles

- `ui/theme.css` owns the application palette, spacing, typography and responsive rules.
- `.streamlit/config.toml` sets native widget and canvas-table colors. Keep the native
  background white: the gray application canvas is applied by CSS.
- `api/server.py` contains the standalone monitor's presentation; use the same palette.
- Main colors: ink `#191e1b`, muted `#737b76`, green `#108455`, pale green `#e6f3ec`,
  workspace `#f3f4f3`, white `#ffffff`. Amber and rose indicate attention/error states.
- Use 12/16/20/24 px spacing, 24 px surface radii, 12 px field radii and pill buttons.
- Headings: 30 px desktop / 25 px mobile; section titles 17 px; body 14 px; labels 12 px.
- Use native Streamlit column gaps. Overriding the gap globally breaks calculated
  column widths and can push the last metric onto a new row.
- Prefer named containers and stable `data-testid` selectors; never target generated
  Emotion class names. Current radio/input/select widgets use React Aria.

## Behavior

The dashboard chart counts today's stored recognition events by local hour. It is not
a count of distinct visitors. The attendance metric continues to show local employees.
The camera overview reads an existing JPEG once per page run; the dedicated monitor
retains its existing refresh loop. Neither UI opens a new RTSP connection.

Directory tables retain search, pagination and profile selection. Profile headings
share one photo/name/status layout. Forms retain their existing database operations.

## Visual Verification

On 2026-09-08 the dashboard, ERP/unknown directories, registration, analytics, API
and monitor were reviewed using browser screenshots. Desktop review used 1440 px;
the dashboard also passed a 390 px mobile check with no document-level horizontal
overflow. Wide tables and mobile navigation keep their own horizontal scrolling.
Streamlit AppTest rendered all five main sections without exceptions.

For future changes, compare screenshots with the approved reference, check one-item
charts and long labels, and inspect desktop/mobile widths before recording completion.
Update only UI/API containers for presentation changes; camera and sync services can
continue running.
