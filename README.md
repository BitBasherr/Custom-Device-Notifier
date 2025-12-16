# Custom Device Notifier

Create a custom `notify.<your_name>` service in Home Assistant that evaluates multiple conditions (across sensors, binary_sensors, device_trackers, inputs, etc.) in priority order and forwards your notification to the first matching underlying service—or to a fallback—without writing extra automations.

**NEW**: Now includes medication tracking with a sidebar panel, CSV logging, and dose monitoring!

## ✨ Features

### Notification Routing
- **Conditional Routing**: Route notifications based on entity states and conditions
- **Smart Select**: Intelligent PC/Phone selection based on device state
- **Priority-based**: First matching target wins
- **Fallback Support**: Always have a backup notification method
- **Live Preview**: Real-time sensor showing current routing target

### 💊 Medication Tracking (NEW)
- **Sidebar Panel**: Dedicated UI for viewing and managing all medications
- **CSV Logging**: Persistent history with timestamps for each medication
- **Dose Monitoring**: Track scheduled vs. taken doses throughout the day
- **Services**: Mark medications taken via automation or manual action
- **Attributes**: Rich sensor data including last taken time, doses today, and compliance ratio
- **Non-scroll Time Pickers**: Easy-to-use date/time selection

[See full medication tracking documentation →](MEDICATION_TRACKING.md)

## 📥 Installation

### HACS

1. Place **hacs.json** at the root of your `custom_components/` folder:
   ```json
   {
     "name": "Custom Device Notifier",
     "content_in_root": false,
     "domain": "custom_device_notifier",
     "homeassistant": "2025.7.0",
     "render_readme": true
   }
   ```
2. Under `custom_components/custom_device_notifier/`, include:
   ```
   manifest.json  
   const.py  
   __init__.py  
   config_flow.py  
   notify.py  
   sensor.py  
   translations/en.json
   ```
3. In HACS → Integrations → **Custom Device Notifier** → Install  
4. Restart Home Assistant

### Manual

1. Copy the `custom_device_notifier` folder into `config/custom_components/`  
2. Restart Home Assistant

## 🔧 Configuration

1. Go to **Settings → Integrations → Add Integration**  
2. Search for and select **Custom Device Notifier**  
3. Follow the wizard:
   1. **Name Your Service**  
      - Enter a human-friendly name (spaces OK)  
      - A snake_case slug for `notify.<slug>` is auto-generated  
   2. **Select Notify Target**  
      - Pick an existing `notify.*` service  
   3. **Define Conditions** for that target:  
      - **Entity**: any sensor, binary_sensor, device_tracker, or `input_*`  
      - **Operator**: `>`, `<`, `>=`, `<=`, `==`, `!=`  
      - **Value**:  
        - Battery sensors get a **0–100 slider**  
        - Other entities get a **dropdown** of `[current_state, unknown or unavailable, unknown, unavailable]`  
      - **Match Mode**: choose **Match All** vs **Match Any**  
      - “Add another condition” or “Done this target”  
   4. “Add another notify target” or “Done targets”  
   5. **Set Priority Order** (first match wins)  
   6. **Pick Fallback** (used if no targets match)  

## ▶️ Usage

Call your new service directly—no extra automations required:
```yaml
service: notify.my_notifier
data:
  title: "Alert"
  message: "Something happened!"
```

A live sensor `sensor.<slug>_current_target` shows which underlying service would fire **right now**.

## 💊 Medication Tracking

Track medications with ease using the integrated medication management system:

### Quick Start

1. **Configure Medications**:
   - Go to your Custom Device Notifier integration
   - Click **Configure**
   - Select **💊 Medication tracking**
   - Add medications with names and schedules (e.g., "08:00,20:00")

2. **Access the Panel**:
   - Find the **Medications** panel in your sidebar (💊 icon)
   - View all medications, their status, and schedule
   - Mark medications as taken with one click

3. **Use in Automations**:
   ```yaml
   service: custom_device_notifier.mark_medication_taken
   data:
     medication_name: "Aspirin"
   ```

### Features
- ✅ **Status Tracking**: See if medications are Not Taken, Partial, or Complete for the day
- ✅ **CSV Logs**: All doses logged to `<config>/custom_components/custom_device_notifier/medication_logs/`
- ✅ **Rich Attributes**: Last Taken at, Doses taken today, Doses Taken/Doses Scheduled (as string like "1/2")
- ✅ **Mark All**: Bulk action to mark all medications taken at once
- ✅ **Custom Times**: Set historical or future doses with date/time pickers

[Full medication tracking documentation →](MEDICATION_TRACKING.md)

## 🛠 Developer-Tools

- **Service Domain**: `custom_device_notifier`  
- **Service**: `evaluate`  
- **Usage**: In Developer Tools → Services, select `custom_device_notifier.evaluate` to dump each condition’s result and the overall match decision to the log.


### Medication Tracking
- **Service**: `mark_medication_taken` - Mark a specific medication as taken
- **Service**: `mark_all_medications_taken` - Mark all medications as taken
- **Sensors**: Each medication gets a sensor at `sensor.medication_<name>` with rich attributes
## 🐞 Debug Logging

To see detailed logs, add to your `configuration.yaml`:
```yaml
logger:
  default: warning
  logs:
    custom_device_notifier: debug
```
Then restart and filter by **Custom Device Notifier** in **Configuration → Logs**.

---

Enjoy powerful, priority-based notifications—no extra automations required!
```
::contentReference[oaicite:0]{index=0}
