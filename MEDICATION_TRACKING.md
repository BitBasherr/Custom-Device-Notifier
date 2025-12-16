# Medication Tracking Feature

## Overview

The Custom Device Notifier integration now includes medication tracking functionality. This allows you to:

- Track multiple medications with scheduled doses
- Log when medications are taken (with CSV history)
- View medication status via a sidebar panel
- Mark medications as taken via services or the UI
- Monitor daily dose compliance

## Configuration

Medications are configured through the integration's options. To configure medications:

1. Go to **Settings → Devices & Services**
2. Find your **Custom Device Notifier** integration
3. Click **Configure**
4. Select **💊 Medication tracking**
5. Add your medications with names and schedules

### Example Configuration (via configuration.yaml if needed)

```yaml
# In your integration options (typically managed via UI)
medications:
  - med_name: "Aspirin"
    med_schedule:
      - "08:00"
      - "20:00"
    med_enabled: true
  
  - med_name: "Vitamin D"
    med_schedule:
      - "09:00"
    med_enabled: true
```

## Medication Sensors

Each medication creates a sensor entity with the following properties:

### Entity ID Format
`sensor.medication_<medication_name_slug>`

Example: `sensor.medication_aspirin`

### States
- **Not Taken**: No doses taken today
- **Partial (X/Y)**: X doses taken out of Y scheduled
- **Complete**: All scheduled doses taken

### Attributes

Each medication sensor includes the following attributes:

| Attribute | Description | Format |
|-----------|-------------|--------|
| `medication_name` | Name of the medication | String |
| `schedule` | List of scheduled times | List of times (e.g., ["08:00", "20:00"]) |
| `scheduled_doses_count` | Number of doses per day | Integer |
| `Last Taken at` | Timestamp of last dose | ISO 8601 timestamp or "Never" |
| `Doses taken today` | List of times doses were taken today | List of times (e.g., ["08:15:30", "20:05:12"]) |
| `Doses Taken/Doses Scheduled` | Fraction as string | String (e.g., "1/2", "2/2") |
| `log_file` | Path to CSV log file | Absolute path |

**Note**: The `Doses Taken/Doses Scheduled` attribute is returned as a **string** (not a number) to display as a fraction like "1/2" or "0/3".

## CSV Logging

Medication history is logged to CSV files with the following details:

### Log Location
Logs are stored in: `<config_dir>/custom_components/custom_device_notifier/medication_logs/`

Each medication has its own CSV file: `<medication_name>.csv`

### Log Format
```csv
Timestamp,Date,Time,Medication
2025-12-16T08:15:30,2025-12-16,08:15:30,Aspirin
2025-12-16T20:05:12,2025-12-16,20:05:12,Aspirin
```

### Columns
- **Timestamp**: Full ISO 8601 timestamp
- **Date**: Date (YYYY-MM-DD)
- **Time**: Time (HH:MM:SS)
- **Medication**: Medication name

## Services

### `custom_device_notifier.mark_medication_taken`

Mark a specific medication as taken.

**Service Data:**

| Field | Required | Description |
|-------|----------|-------------|
| `medication_name` | Yes | Name of the medication |
| `timestamp` | No | ISO 8601 timestamp (defaults to now) |

**Example:**

```yaml
service: custom_device_notifier.mark_medication_taken
data:
  medication_name: "Aspirin"
```

**With custom timestamp:**

```yaml
service: custom_device_notifier.mark_medication_taken
data:
  medication_name: "Aspirin"
  timestamp: "2025-12-16T08:00:00"
```

### `custom_device_notifier.mark_all_medications_taken`

Mark all configured medications as taken at once.

**Service Data:** None

**Example:**

```yaml
service: custom_device_notifier.mark_all_medications_taken
```

## Sidebar Panel

A dedicated sidebar panel is available for managing medications:

### Features
- View all medications and their status
- See schedule and doses taken
- Mark individual medications as taken
- Mark all medications as taken at once
- Custom time selection (non-scroll wheel date/time pickers)
- Real-time updates

### Accessing the Panel
The panel appears in the sidebar with the icon 💊 and title "Medications" when medications are configured.

## Time Input

The time input selector uses **standard HTML5 date and time inputs** (non-scroll wheel):
- Date picker: Uses native browser date selector
- Time picker: Uses native browser time selector

This provides a consistent, accessible experience across different devices and browsers.

## Automations

### Example: Daily Medication Reminder

```yaml
automation:
  - alias: "Morning Medication Reminder"
    trigger:
      - platform: time
        at: "08:00:00"
    condition:
      - condition: template
        value_template: >
          {{ state_attr('sensor.medication_aspirin', 'Doses Taken/Doses Scheduled').split('/')[0] == '0' }}
    action:
      - service: notify.mobile_app_phone
        data:
          title: "Medication Reminder"
          message: "Time to take your morning Aspirin"
```

### Example: Mark Medication from Notification Action

```yaml
automation:
  - alias: "Mark Medication from Action"
    trigger:
      - platform: event
        event_type: mobile_app_notification_action
        event_data:
          action: MARK_MED_TAKEN
    action:
      - service: custom_device_notifier.mark_medication_taken
        data:
          medication_name: "{{ trigger.event.data.medication }}"
```

## Dashboard Cards

### Medication Status Card

```yaml
type: entities
entities:
  - sensor.medication_aspirin
  - sensor.medication_vitamin_d
title: Medication Status
```

### Detailed Medication Card

```yaml
type: custom:button-card
entity: sensor.medication_aspirin
name: Aspirin
show_state: true
show_label: true
label: |
  [[[
    return 'Last taken: ' + entity.attributes['Last Taken at'];
  ]]]
tap_action:
  action: call-service
  service: custom_device_notifier.mark_medication_taken
  service_data:
    medication_name: Aspirin
```

## Backward Compatibility

All existing Custom Device Notifier features (notification routing, smart select, TTS, messages bridge) remain fully functional. The medication tracking feature is **optional** and does not affect existing configurations.

## Troubleshooting

### Medication sensors not appearing
- Check that medications are configured in the integration options
- Reload the integration
- Check Home Assistant logs for errors

### CSV logs not being created
- Check file permissions in the config directory
- Verify the path in the sensor's `log_file` attribute
- Check Home Assistant logs for write errors

### Panel not showing
- Ensure at least one medication is configured
- Clear browser cache
- Restart Home Assistant

## Future Enhancements

Planned features:
- Medication refill reminders
- Dosage tracking
- Export log data
- Integration with calendar for scheduling
- Multi-device synchronization
