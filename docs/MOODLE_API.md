# Moodle API Reference

Quick reference for the Moodle web service endpoints and fields that `course_sync.py` actually hits.

All requests go to:

```
GET {base_url}/webservice/rest/server.php
    ?wstoken={token}
    &wsfunction={function}
    &moodlewsrestformat=json
    &{additional params}
```

---

## Token requirements

- Token type: **Moodle mobile web service**
- To get one: Profile > Preferences > Security keys > "Moodle mobile web service" > Reset / copy.
- Gives read access to everything the user can see.
- Passed as `wstoken` on every request.

---

## Endpoints

### `core_webservice_get_site_info`

No additional parameters.

**Fields read:**

| Field | Type | Used for |
|---|---|---|
| `userid` | int | User ID, passed to `core_enrol_get_users_courses` |
| `fullname` | str | Displayed at startup ("Logged in as: ...") |

---

### `core_enrol_get_users_courses`

**Parameters:** `userid` (int)

**Returns:** Array of course objects.

**Fields read:**

| Field | Type | Used for |
|---|---|---|
| `id` | int | Course ID, passed to `core_course_get_contents` |
| `shortname` | str | Prefix-matched against `code` values in config |
| `fullname` | str | Displayed in course header output |

---

### `core_course_get_contents`

**Parameters:** `courseid` (int)

**Returns:** Array of section objects. Each section has a `modules` array.

**Fields read from each section:**

| Field | Type | Used for |
|---|---|---|
| `name` | str | Mapped to a folder name (WeekN / Assessments / skipped) |
| `modules` | array | List of modules in this section |

**Fields read from each module:**

| Field | Type | Used for |
|---|---|---|
| `modname` | str | Dispatch to handler (`resource`, `folder`, `page`, `label`, `url`) |
| `name` | str | Display name for `_index.md` rows |
| `description` | str (HTML) | `label` handler: parsed for `<a href>` and `<img src>` links |
| `contents` | array | `resource`, `folder`, `page`, `url` handlers: file entries |

**Fields read from each `contents` entry:**

| Field | Type | Used for |
|---|---|---|
| `type` | str | Filter to `"file"` entries only (`resource`, `folder`, `page`) |
| `filename` | str | Local filename (URL-decoded before use) |
| `fileurl` | str | Download URL, converted via `make_download_url()` before fetching |

---

## Failure modes

| Symptom | Cause |
|---|---|
| `RuntimeError: Moodle API error: ...` | API returned JSON with an `"exception"` key. Usually the token's invalid/expired, or that function isn't allowed for it. |
| HTTP 404 on file download | Likely the double-webservice URL bug. A URL that already has `/webservice/pluginfile.php/` in it didn't get caught, so the prefix got slapped on twice. See [DEV_GUIDE.md, URL conversion section](DEV_GUIDE.md#6-url-conversion-and-the-double-webservice-bug). |
| `service not available` or similar in exception message | Either the "Moodle mobile web service" isn't enabled on the instance, or the token doesn't have access to that function. |
| Empty `modules` list for a section | Content's hidden or access-restricted in Moodle. The section still gets processed but nothing downloads. |
| `core_enrol_get_users_courses` returns empty list | User has no enrolled courses, or the token doesn't have enrolment read permission. |
