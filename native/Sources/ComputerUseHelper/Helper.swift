import AppKit
import ApplicationServices
import CoreGraphics
import ScreenCaptureKit

typealias JSON = [String: Any]

struct Failure: Error, CustomStringConvertible {
    let description: String
    init(_ description: String) { self.description = description }
}

func attr(_ element: AXUIElement, _ name: String) -> CFTypeRef? {
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success else { return nil }
    return value
}

func stringAttr(_ element: AXUIElement, _ name: String) -> String {
    guard let value = attr(element, name) else { return "" }
    if let text = value as? String { return String(text.prefix(1500)) }
    if let number = value as? NSNumber { return number.stringValue }
    return ""
}

func frameOf(_ element: AXUIElement) -> CGRect? {
    guard let p = attr(element, kAXPositionAttribute), CFGetTypeID(p) == AXValueGetTypeID(),
          let s = attr(element, kAXSizeAttribute), CFGetTypeID(s) == AXValueGetTypeID() else { return nil }
    var point = CGPoint.zero, size = CGSize.zero
    guard AXValueGetValue(p as! AXValue, .cgPoint, &point),
          AXValueGetValue(s as! AXValue, .cgSize, &size) else { return nil }
    return CGRect(origin: point, size: size)
}

func frameJSON(_ frame: CGRect) -> JSON {
    ["x": frame.minX, "y": frame.minY, "width": frame.width, "height": frame.height]
}

func closeFrame(_ lhs: CGRect, _ rhs: CGRect) -> Bool {
    abs(lhs.minX - rhs.minX) < 2 && abs(lhs.minY - rhs.minY) < 2 &&
    abs(lhs.width - rhs.width) < 2 && abs(lhs.height - rhs.height) < 2
}

@MainActor final class Helper {
    var elements: [String: AXUIElement] = [:]
    var signatures: [String: String] = [:]
    var generation = ""
    var selectedWindow: AXUIElement?
    var selectedPID: pid_t = 0
    var selectedWindowID: CGWindowID = 0
    var selectedBundle = ""
    var selectedFrame = CGRect.zero
    var imageSize = CGSize.zero

    func signature(_ element: AXUIElement) -> String {
        if stringAttr(element, kAXSubroleAttribute) == "AXSecureTextField" { return "[secure field]" }
        return [kAXRoleAttribute, kAXSubroleAttribute, kAXTitleAttribute, kAXDescriptionAttribute, kAXValueAttribute]
            .map { stringAttr(element, $0) }.joined(separator: "\u{001F}")
    }

    func doctor() -> JSON {
        ["accessibility": AXIsProcessTrusted(),
         "screen_recording": CGPreflightScreenCaptureAccess(),
         "platform": "macOS", "native_version": "0.1.0"]
    }

    func windows() -> [JSON] {
        let apps = Dictionary(NSWorkspace.shared.runningApplications.compactMap { app -> (pid_t, NSRunningApplication)? in
            guard app.activationPolicy == .regular, app.bundleIdentifier != nil else { return nil }
            return (app.processIdentifier, app)
        }, uniquingKeysWith: { a, _ in a })
        let raw = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [JSON] ?? []
        return raw.compactMap { item in
            guard let pid = item[kCGWindowOwnerPID as String] as? Int32, let app = apps[pid],
                  let wid = item[kCGWindowNumber as String] as? UInt32,
                  let layer = item[kCGWindowLayer as String] as? Int, layer == 0,
                  let bounds = item[kCGWindowBounds as String] as? [String: Any],
                  let rect = CGRect(dictionaryRepresentation: bounds as CFDictionary), rect.width > 100, rect.height > 60 else { return nil }
            return ["bundle_id": app.bundleIdentifier!, "app": app.localizedName ?? app.bundleIdentifier!,
                    "pid": pid, "window_id": wid,
                    "title": item[kCGWindowName as String] as? String ?? "", "frame": frameJSON(rect)]
        }
    }

    func target(_ request: JSON) throws -> (NSRunningApplication, CGWindowID, CGRect) {
        guard let bundle = request["bundle_id"] as? String,
              let wid = request["window_id"] as? UInt32,
              let item = windows().first(where: { ($0["window_id"] as? UInt32) == wid && ($0["bundle_id"] as? String) == bundle }),
              let pid = item["pid"] as? Int32, let app = NSRunningApplication(processIdentifier: pid),
              let f = item["frame"] as? JSON,
              let x = f["x"] as? Double, let y = f["y"] as? Double,
              let w = f["width"] as? Double, let h = f["height"] as? Double else {
            throw Failure("Target window is closed, hidden, or unavailable. Refresh the window list.")
        }
        return (app, wid, CGRect(x: x, y: y, width: w, height: h))
    }

    func axWindow(_ pid: pid_t, frame: CGRect) throws -> AXUIElement {
        let app = AXUIElementCreateApplication(pid)
        AXUIElementSetMessagingTimeout(app, 2)
        let windows = attr(app, kAXWindowsAttribute) as? [AXUIElement] ?? []
        guard let window = windows.first(where: { frameOf($0).map { closeFrame($0, frame) } ?? false }) else {
            throw Failure("This window does not expose a matching accessibility window. Choose another window.")
        }
        return window
    }

    func walk(_ element: AXUIElement, depth: Int, rows: inout [JSON], seen: inout Set<CFHashCode>) {
        guard depth < 12, rows.count < 350 else { return }
        let hash = CFHash(element)
        guard seen.insert(hash).inserted else { return }
        let role = stringAttr(element, kAXRoleAttribute)
        let subrole = stringAttr(element, kAXSubroleAttribute)
        let id = "e\(rows.count + 1)"
        elements[id] = element
        signatures[id] = signature(element)
        var actionNames: CFArray?
        AXUIElementCopyActionNames(element, &actionNames)
        let secure = subrole == "AXSecureTextField"
        var row: JSON = ["id": id, "depth": depth, "role": role, "subrole": subrole,
                         "title": stringAttr(element, kAXTitleAttribute),
                         "description": stringAttr(element, kAXDescriptionAttribute),
                         "value": secure ? "[secure field]" : stringAttr(element, kAXValueAttribute),
                         "enabled": (attr(element, kAXEnabledAttribute) as? Bool) ?? true,
                         "actions": actionNames as? [String] ?? []]
        if let f = frameOf(element) {
            row["frame"] = frameJSON(f.offsetBy(dx: -selectedFrame.minX, dy: -selectedFrame.minY))
        }
        rows.append(row)
        if secure { return }
        for child in attr(element, kAXChildrenAttribute) as? [AXUIElement] ?? [] {
            walk(child, depth: depth + 1, rows: &rows, seen: &seen)
        }
    }

    func observe(_ request: JSON) async throws -> JSON {
        generation = ""; elements.removeAll(); signatures.removeAll()
        guard AXIsProcessTrusted() else { throw Failure("Accessibility permission is missing. Open the permissions panel and enable your terminal or launcher.") }
        guard CGPreflightScreenCaptureAccess() else { throw Failure("Screen Recording permission is missing. Enable your terminal or launcher in System Settings and restart it.") }
        let (app, wid, frame) = try target(request)
        let window = try axWindow(app.processIdentifier, frame: frame)
        selectedPID = app.processIdentifier; selectedWindowID = wid
        selectedBundle = app.bundleIdentifier!; selectedFrame = frame; selectedWindow = window
        var rows: [JSON] = [], seen = Set<CFHashCode>()
        walk(window, depth: 0, rows: &rows, seen: &seen)

        let content = try await SCShareableContent.excludingDesktopWindows(true, onScreenWindowsOnly: true)
        guard let scWindow = content.windows.first(where: { $0.windowID == wid }) else { throw Failure("Window is not available for capture.") }
        let filter = SCContentFilter(desktopIndependentWindow: scWindow)
        let config = SCStreamConfiguration()
        let scale = min(1.0, 1440.0 / frame.width)
        config.width = max(1, Int((frame.width * scale).rounded()))
        config.height = max(1, Int((frame.height * scale).rounded()))
        config.showsCursor = false; config.ignoreShadowsSingleWindow = true
        config.scalesToFit = true
        let image = try await SCScreenshotManager.captureImage(contentFilter: filter, configuration: config)
        let bitmap = NSBitmapImageRep(cgImage: image)
        guard let png = bitmap.representation(using: .png, properties: [:]) else { throw Failure("PNG encoding failed.") }
        let (_, _, afterFrame) = try target(request)
        guard closeFrame(frame, afterFrame) else { throw Failure("Window moved during observation. Observe again.") }
        imageSize = CGSize(width: image.width, height: image.height)
        generation = UUID().uuidString
        return ["generation": generation,
                "target": ["bundle_id": selectedBundle, "pid": selectedPID, "window_id": wid,
                           "title": stringAttr(window, kAXTitleAttribute), "frame": frameJSON(frame)],
                "elements": rows, "truncated": rows.count >= 350,
                "screenshot": ["data_url": "data:image/png;base64," + png.base64EncodedString(),
                               "width": image.width, "height": image.height]]
    }

    func focus(_ app: NSRunningApplication, _ window: AXUIElement) async throws {
        app.activate(options: [])
        AXUIElementPerformAction(window, kAXRaiseAction as CFString)
        try await Task.sleep(nanoseconds: 250_000_000)
        guard NSWorkspace.shared.frontmostApplication?.processIdentifier == selectedPID else { throw Failure("Could not activate target app.") }
        let axApp = AXUIElementCreateApplication(selectedPID)
        guard let focused = attr(axApp, kAXFocusedWindowAttribute), CFGetTypeID(focused) == AXUIElementGetTypeID(),
              CFEqual(focused, window) else { throw Failure("Target window is not focused; no input was sent.") }
    }

    func point(_ action: JSON, prefix: String = "") throws -> CGPoint {
        guard let x = action[prefix + "x"] as? Double, let y = action[prefix + "y"] as? Double,
              x.isFinite, y.isFinite, x >= 0, y >= 0, x < imageSize.width, y < imageSize.height else {
            throw Failure("Coordinates must be within the latest screenshot.")
        }
        return CGPoint(x: selectedFrame.minX + x * selectedFrame.width / imageSize.width,
                       y: selectedFrame.minY + y * selectedFrame.height / imageSize.height)
    }

    func click(_ p: CGPoint, count: Int = 1) throws {
        // Refuse a click through a different app, popup, or overlapping window.
        let system = AXUIElementCreateSystemWide()
        var hit: AXUIElement?
        guard AXUIElementCopyElementAtPosition(system, Float(p.x), Float(p.y), &hit) == .success, let hit else {
            throw Failure("Cannot verify the element under this point.")
        }
        var pid: pid_t = 0
        AXUIElementGetPid(hit, &pid)
        guard pid == selectedPID else { throw Failure("Another application covers the click location.") }
        let containing = attr(hit, kAXWindowAttribute)
        guard let containing, let window = selectedWindow, CFEqual(containing, window) || CFEqual(hit, window) else {
            throw Failure("Click location belongs to another window.")
        }
        for n in 1...count {
            guard let down = CGEvent(mouseEventSource: nil, mouseType: .leftMouseDown, mouseCursorPosition: p, mouseButton: .left),
                  let up = CGEvent(mouseEventSource: nil, mouseType: .leftMouseUp, mouseCursorPosition: p, mouseButton: .left) else {
                throw Failure("Could not create mouse event.")
            }
            down.setIntegerValueField(.mouseEventClickState, value: Int64(n))
            up.setIntegerValueField(.mouseEventClickState, value: Int64(n))
            down.post(tap: .cghidEventTap); up.post(tap: .cghidEventTap)
        }
    }

    func act(_ request: JSON) async throws -> JSON {
        guard let expected = request["generation"] as? String, !generation.isEmpty, expected == generation,
              let action = request["action"] as? JSON, let kind = action["kind"] as? String,
              let window = selectedWindow else { throw Failure("Stale observation. Observe again before acting.") }
        let (app, wid, frame) = try target(request)
        guard wid == selectedWindowID, app.processIdentifier == selectedPID,
              app.bundleIdentifier == selectedBundle, closeFrame(frame, selectedFrame) else {
            generation = ""; throw Failure("Target changed since observation. Observe again.")
        }
        // A generation is single use, including failed actions.
        generation = ""
        try await focus(app, window)
        let (_, _, focusedFrame) = try target(request)
        guard closeFrame(focusedFrame, selectedFrame) else { throw Failure("Window moved while focusing. Observe again.") }
        let element = (action["element_id"] as? String).flatMap { elements[$0] }
        if let id = action["element_id"] as? String, elements[id] == nil { throw Failure("Unknown element ID.") }
        if let element, let id = action["element_id"] as? String {
            guard signatures[id] == signature(element),
                  let container = attr(element, kAXWindowAttribute), CFEqual(container, window) else {
                throw Failure("The element changed or moved to another window. Observe again.")
            }
            guard (attr(element, kAXEnabledAttribute) as? Bool) ?? true else { throw Failure("Element is now disabled.") }
        }
        if let element, stringAttr(element, kAXSubroleAttribute) == "AXSecureTextField" { throw Failure("Secure fields require manual input.") }
        switch kind {
        case "click":
            if let element {
                guard AXUIElementPerformAction(element, kAXPressAction as CFString) == .success else {
                    throw Failure("Element does not support AXPress. Observe again and use screenshot coordinates.")
                }
            } else { try click(point(action)) }
        case "double_click": try click(point(action), count: 2)
        case "set_value":
            guard let element, let value = action["text"] as? String, value.count <= 4000 else { throw Failure("set_value requires an element and text (max 4000 characters).") }
            guard AXUIElementSetAttributeValue(element, kAXValueAttribute as CFString, value as CFString) == .success else {
                throw Failure("This element does not support setting a value. Use type_text after focusing a text field.")
            }
        case "type_text":
            guard let text = action["text"] as? String, text.count <= 4000 else { throw Failure("Invalid text.") }
            if let element {
                guard AXUIElementSetAttributeValue(element, kAXFocusedAttribute as CFString, kCFBooleanTrue) == .success else { throw Failure("Cannot focus text element.") }
            }
            guard let focused = attr(AXUIElementCreateApplication(selectedPID), kAXFocusedUIElementAttribute),
                  CFGetTypeID(focused) == AXUIElementGetTypeID() else { throw Failure("No focused text field.") }
            let field = focused as! AXUIElement
            let role = stringAttr(field, kAXRoleAttribute)
            guard ["AXTextField", "AXTextArea", "AXComboBox"].contains(role),
                  stringAttr(field, kAXSubroleAttribute) != "AXSecureTextField" else { throw Failure("Text input is only allowed in an ordinary text field.") }
            // Send Unicode directly; never replace the user's clipboard.
            for chunk in text.utf16.chunks(of: 20) {
                guard let down = CGEvent(keyboardEventSource: nil, virtualKey: 0, keyDown: true),
                      let up = CGEvent(keyboardEventSource: nil, virtualKey: 0, keyDown: false) else { throw Failure("Could not create key event.") }
                chunk.withUnsafeBufferPointer { b in
                    down.keyboardSetUnicodeString(stringLength: b.count, unicodeString: b.baseAddress)
                    up.keyboardSetUnicodeString(stringLength: b.count, unicodeString: b.baseAddress)
                }
                down.post(tap: .cghidEventTap); up.post(tap: .cghidEventTap)
            }
        case "press_key":
            let keys: [String: (CGKeyCode, CGEventFlags)] = ["enter": (36, []), "tab": (48, []), "escape": (53, []),
                "backspace": (51, []), "delete": (117, []), "left": (123, []), "right": (124, []),
                "down": (125, []), "up": (126, []), "home": (115, []), "end": (119, []),
                "page_up": (116, []), "page_down": (121, []), "select_all": (0, .maskCommand),
                "undo": (6, .maskCommand), "redo": (6, [.maskCommand, .maskShift]), "save": (1, .maskCommand)]
            guard let key = action["key"] as? String, let (code, flags) = keys[key],
                  let down = CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: true),
                  let up = CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: false) else { throw Failure("Unsupported key.") }
            down.flags = flags; up.flags = flags
            down.post(tap: .cghidEventTap); up.post(tap: .cghidEventTap)
        case "scroll":
            let p = try point(action)
            let amount = min(800, max(-800, (action["scroll_y"] as? Int) ?? 0))
            // Verify the target under the cursor before scrolling, without clicking it.
            var hit: AXUIElement?
            guard AXUIElementCopyElementAtPosition(AXUIElementCreateSystemWide(), Float(p.x), Float(p.y), &hit) == .success,
                  let hit, let container = attr(hit, kAXWindowAttribute), CFEqual(container, window) else { throw Failure("Scroll point is outside the target window.") }
            CGEvent(mouseEventSource: nil, mouseType: .mouseMoved, mouseCursorPosition: p, mouseButton: .left)?.post(tap: .cghidEventTap)
            guard let event = CGEvent(scrollWheelEvent2Source: nil, units: .pixel, wheelCount: 1, wheel1: Int32(-amount), wheel2: 0, wheel3: 0) else { throw Failure("Could not create scroll event.") }
            event.post(tap: .cghidEventTap)
        default: throw Failure("Unsupported action.")
        }
        try await Task.sleep(nanoseconds: 250_000_000)
        return ["ok": true, "kind": kind]
    }

    func handle(_ request: JSON) async throws -> JSON {
        switch request["op"] as? String {
        case "doctor": return doctor()
        case "request_permissions":
            let options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
            _ = AXIsProcessTrustedWithOptions(options)
            _ = CGRequestScreenCaptureAccess()
            return doctor()
        case "windows": return ["windows": windows()]
        case "observe": return try await observe(request)
        case "act": return try await act(request)
        default: throw Failure("Unknown operation.")
        }
    }
}

extension String.UTF16View {
    func chunks(of count: Int) -> [[UInt16]] {
        let values = Array(self)
        var result: [[UInt16]] = [], start = 0
        while start < values.count {
            var end = Swift.min(start + count, values.count)
            if end < values.count && (0xD800...0xDBFF).contains(values[end - 1]) { end -= 1 }
            result.append(Array(values[start..<end])); start = end
        }
        return result
    }
}

@main
struct Main {
    @MainActor static func main() async {
        let helper = Helper()
        while let line = readLine() {
            do {
                guard let data = line.data(using: .utf8), let request = try JSONSerialization.jsonObject(with: data) as? JSON else { throw Failure("Invalid JSON object.") }
                let result = try await helper.handle(request)
                let output = try JSONSerialization.data(withJSONObject: ["ok": true, "result": result], options: [.sortedKeys])
                print(String(decoding: output, as: UTF8.self))
            } catch {
                let data = try! JSONSerialization.data(withJSONObject: ["ok": false, "error": String(describing: error)])
                print(String(decoding: data, as: UTF8.self))
            }
            fflush(stdout)
        }
    }
}
