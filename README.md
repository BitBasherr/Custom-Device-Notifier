# Custom Device Notifier

Create a custom `notify.<your_name>` service in Home Assistant that evaluates multiple conditions (across sensors, binary_sensors, device_trackers, inputs, etc.) in priority order and forwards your notification to the first matching underlying service—or to a fallback—without writing extra automations.

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

## 🛠 Developer-Tools

- **Service Domain**: `custom_device_notifier`  
- **Service**: `evaluate`  
- **Usage**: In Developer Tools → Services, select `custom_device_notifier.evaluate` to dump each condition’s result and the overall match decision to the log.

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
