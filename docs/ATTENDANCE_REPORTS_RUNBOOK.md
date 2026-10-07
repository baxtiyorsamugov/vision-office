# Local attendance reports

The **Отчёты** page reads the PostgreSQL `recognition_events` table on this
device. It does not call EduSchool, submit attendance, or touch RTSP/FaceID.
EduSchool employees and students are included by default; local employees can
be added with the switch. Legacy ERP and unknown people are excluded.

Choose Today, Yesterday, Last 7 days, Current month, or a custom period of at
most 31 local calendar days. Click **Сформировать отчёт**. The page previews
daily and person totals and offers:

- PDF: manager summary and full person tables, with local `Asia/Tashkent` time.
- Excel: summary, daily totals, employees, students, visits, and **Все фиксации**.
  The last sheet includes every saved recognition event in the selected period,
  even repeated camera detections. It does not include every video frame.

Visits are paired by stable person ID, not name. The first entry opens a visit;
the first exit within 18 hours closes it. Repeated entries on the same local day
and exits within five minutes of a completed exit remain visible in the raw
sheet but do not increase visit counts. An entry without exit and an exit
without entry are reported as incomplete, never assigned a fabricated duration.
Cross-midnight exits are paired with the entry day when the event is available.
The report reads up to 18 hours outside the selected dates solely to classify
boundary events; the raw-events sheet contains only events inside the chosen
local dates. The report refuses more than 250,000 fetched events and asks the
operator to narrow the period.

"Employees" and "Students" count distinct IDs observed by cameras, not all
active people in the catalog. No camera observation is not proof of absence;
the report does not claim absence or lateness without an authoritative schedule.
EduSchool API delivery status is separate from local attendance. Confidence,
photos, embeddings, API keys, and response bodies are never exported.

Files are generated on demand and downloaded through the local UI. They are not
automatically stored in the application data directory or sent to EduSchool.
Treat downloaded files as personal data: limit access and delete obsolete
copies according to the organization's retention policy. The original events
remain in PostgreSQL; event photos follow their separate retention policy.
