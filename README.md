# Publication Tracker

**WORK IN PROGRESS**

The Publication Tracker is designed to help FABRIC users and operators keep track of publications that have in some way utilized FABRIC services to do their work.


**DISCLAIMER: The code herein may not be up to date nor compliant with the most recent package and/or security notices. The frequency at which this code is reviewed and updated is based solely on the lifecycle of the project for which it was written to support, and is not actively maintained outside of that scope. Use at your own risk.**

## Simple Publication format

At this time the Publication Tracker is operating in a "simple mode", and only allows for basic data entry.

- **authors**: array of author names in string format
- **link**: URL that leads to the publication in string format
- **project_name**: free text representation of a FABRIC project in string format
- **project_uuid**: UUID4 reference to a FABRIC project in string format
- **title**: free text representation of the title in string string
- **year**: free text representation of the year in string format

```
{
  "authors": [
    "string"
  ],
  "link": "string",
  "project_name": "string",
  "project_uuid": "string",
  "title": "string",
  "year": "string"
}
```
