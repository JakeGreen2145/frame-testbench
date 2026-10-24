# Independent controller property diagnostic

`frame-controller-properties` reads controller metadata through public OpenVR APIs. It is a separate executable; building or running it does not replace the input proxy or `frame-observe`. It has no lease, pose, input, role-assignment, streaming or runtime-settings controls.

Build and run on a Linux machine with the matching installed SDK/runtime:

```sh
sh scripts/build-controller-properties.sh
./build/frame-controller-properties
```

Run only when an operator selects a read-only runtime probe. It initializes a background OpenVR client, reads `IVRSystem`, and closes that client. Its fixed five-second timeout covers library loading, initialization, getters and shutdown. A timeout exits124 with an error rather than waiting on a hung SDK shutdown. Wrong arguments fail before SDK initialization. No arguments are accepted and no property is retried.

The JSON snapshot enumerates at most64 controller-class devices, including disconnected ones. Each row reports device index/class, actual runtime role, role hint, hand-selection priority, registered device type, controller type, input profile path, driver, model, serial and standing-space pose. Every integer property has `value` and native `error`; every string property also has `required_bytes`. Strings use one256-byte buffer. Native errors, oversized results, missing terminators and invalid zero byte counts produce null values with errors. Nonfinite pose values produce null.

`actual_role` is the runtime's current answer. `role_hint` is a property read with its own error; the helper never treats a preferred role as an assigned role. A missing hand-selection priority is unknown, not a zero priority. A missing registered device type is evidence of an unavailable property, not proof of the cause of a role or forwarding failure. Compare the synthetic devices with physical controllers in the same snapshot before proposing a controlled change.

The property names and methods are verified in `vendor/openvr.h`, `IVRSystem_026`: `GetInt32TrackedDeviceProperty`, `GetStringTrackedDeviceProperty`, `GetControllerRoleForTrackedDeviceIndex`, `GetTrackedDeviceClass` and `GetDeviceToAbsoluteTrackingPose`. Role hint is property3007, hand-selection priority7002, registered device type1036, controller type7000 and input profile path1037. The role enum's Invalid value0 remains an observed value, separate from any property error. [Valve's driver documentation](https://github.com/ValveSoftware/openvr/blob/master/docs/Driver_API_Documentation.md) describes controller roles and device properties.

Results are private diagnostic evidence and may include controller serials. `controller_forwarding` stays unknown. A valid connected pose and successful metadata getters prove neither VRLink export nor original game input receipt.

Controlled checks compile this exact executable and a fake public-header OpenVR library whose unused methods abort. They verify an Invalid actual role alongside a valid RightHand hint, negative priorities, missing/error metadata, bounded no-retry strings, disconnected devices, empty enumeration, pre-initialization argument rejection and a hanging getter deadline. They do not load SteamVR:

```sh
python3 -m unittest discover -s tests -p test_controller_properties.py -v
```

For an isolated one-off headset build, the operator can stage `native/controller_properties.cpp`, `native/sdk.hpp` and `vendor/openvr.h` in the same relative directory layout, then compile only this executable:

```sh
g++ -std=c++17 -O2 -Wall -Wextra -Wpedantic native/controller_properties.cpp -ldl -o frame-controller-properties
./frame-controller-properties
```

Staging source, compiling on the headset and running the real observer require separate operator execution. No service, input proxy, manifest, binding or controller setting needs replacement or restart. A host x86-64 build must not be deployed as an aarch64 binary.
