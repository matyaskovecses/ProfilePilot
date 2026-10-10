import WebKit
import Foundation
let pat = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "RTC|ICE|Peer|MediaDevices|Push|ApplePay|Quota|Notification|Capture|Proxy|Safari"
for selName in ["_features", "_experimentalFeatures", "_internalDebugFeatures"] {
    let sel = NSSelectorFromString(selName)
    guard (WKPreferences.self as AnyObject).responds(to: sel),
          let list = (WKPreferences.self as AnyObject).perform(sel)?.takeUnretainedValue() as? [NSObject] else { print("\(selName): n/a"); continue }
    print("== \(selName): \(list.count) features")
    for f in list {
        let key = (f.value(forKey: "key") as? String) ?? "?"
        let name = (f.value(forKey: "name") as? String) ?? ""
        let def = (f.value(forKey: "defaultValue") as? Bool).map { $0 ? "on" : "off" } ?? "?"
        if key.range(of: pat, options: [.regularExpression, .caseInsensitive]) != nil || name.range(of: pat, options: [.regularExpression, .caseInsensitive]) != nil {
            print("  \(key) [default \(def)] \(name)")
        }
    }
}
var count: UInt32 = 0
if let methods = class_copyMethodList(WKPreferences.self, &count) {
    let names = (0..<Int(count)).map { NSStringFromSelector(method_getName(methods[$0])) }
    print("== WKPreferences setters matching:", names.filter { $0.hasPrefix("_set") && $0.range(of: pat, options: [.regularExpression, .caseInsensitive]) != nil }.sorted())
}
if let cls = NSClassFromString("_WKWebsiteDataStoreConfiguration"), let m = class_copyMethodList(cls, &count) {
    let names = (0..<Int(count)).map { NSStringFromSelector(method_getName(m[$0])) }
    print("== _WKWebsiteDataStoreConfiguration:", names.filter { $0.range(of: "uota|roxy|ICE|RTC|Push", options: [.regularExpression]) != nil }.sorted())
}
