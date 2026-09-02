//! 原生音频设备命令（Tauri）。
//!
//! - macOS：通过 CoreAudio 枚举输入/输出设备，并把「系统默认输出设备」切到用户
//!   选择的扬声器 —— A 线 LiveKit 远端 <audio> 与 B 线同传 WebAudio 都走系统
//!   输出，因此切换后两条线全部生效。
//! - Windows/Linux：暂无原生实现，返回空列表；前端会自动回退到 Web 枚举
//!   （enumerateDevices），扬声器输出跟随系统默认。

use serde::Serialize;

#[derive(Serialize, Clone)]
pub struct AudioDevice {
    pub id: String,
    pub name: String,
    pub is_default: bool,
}

#[cfg(target_os = "macos")]
mod macos {
    use super::AudioDevice;
    use coreaudio_sys::{
        kAudioDevicePropertyDeviceIsAlive, kAudioDevicePropertyDeviceUID,
        kAudioDevicePropertyStreams, kAudioDevicePropertyTransportType,
        kAudioHardwarePropertyDefaultInputDevice, kAudioHardwarePropertyDefaultOutputDevice,
        kAudioHardwarePropertyDevices, kAudioObjectPropertyElementMain,
        kAudioObjectPropertyScopeGlobal, kAudioObjectPropertyScopeInput,
        kAudioObjectSystemObject,
        CFRelease, CFStringGetCString, CFStringGetLength, CFStringGetMaximumSizeForEncoding,
        CFStringRef, CFStringEncoding, AudioObjectID,
    };
    use std::ffi::CStr;
    use std::mem::size_of;
    use std::os::raw::c_void;
    use std::ptr;

    const CFSTR_UTF8: CFStringEncoding = 0x08000100; // kCFStringEncodingUTF8

    unsafe fn cfstring_to_string(cf: CFStringRef) -> Option<String> {
        if cf.is_null() {
            return None;
        }
        let len = CFStringGetLength(cf);
        if len == 0 {
            return Some(String::new());
        }
        let max = CFStringGetMaximumSizeForEncoding(len, CFSTR_UTF8);
        if max <= 0 {
            return None;
        }
        // CFIndex 是 c_long：clamp 到合理上界再转 usize。
        let capacity = (max as i128).min(1 << 20) as usize + 1;
        let mut buf = vec![0u8; capacity];
        let written = CFStringGetCString(
            cf,
            buf.as_mut_ptr() as *mut i8,
            max,
            CFSTR_UTF8,
        );
        if written != 0 {
            Some(CStr::from_ptr(buf.as_ptr() as *const i8).to_string_lossy().into_owned())
        } else {
            None
        }
    }

    /// 读取设备某 CFString 属性（device UID / 名称）。
    ///
    /// CoreAudio 的字符串属性是把一个 `CFStringRef`（指针值）写入 outData，
    /// 而不是把 CFString 对象本身铺进缓冲区 —— 必须用 `CFStringRef*` 变量接收，
    /// 再按约定对取回的引用执行 `CFRelease`。直接解引用缓冲区会导致野指针崩溃。
    unsafe fn get_device_string(device: AudioObjectID, selector: u32, scope: u32) -> Option<String> {
        let addr = coreaudio_sys::AudioObjectPropertyAddress {
            mSelector: selector,
            mScope: scope,
            mElement: kAudioObjectPropertyElementMain,
        };
        let mut cf: CFStringRef = ptr::null();
        let mut size = size_of::<CFStringRef>() as u32;
        let status = coreaudio_sys::AudioObjectGetPropertyData(
            device,
            &addr,
            0,
            ptr::null(),
            &mut size,
            &mut cf as *mut CFStringRef as *mut c_void,
        );
        if status != 0 || cf.is_null() {
            return None;
        }
        let out = cfstring_to_string(cf);
        CFRelease(cf as coreaudio_sys::CFTypeRef);
        out
    }

    unsafe fn device_alive(device: AudioObjectID) -> bool {
        let mut alive: u32 = 0;
        let mut size = size_of::<u32>() as u32;
        let addr = coreaudio_sys::AudioObjectPropertyAddress {
            mSelector: kAudioDevicePropertyDeviceIsAlive,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain,
        };
        coreaudio_sys::AudioObjectGetPropertyData(
            device,
            &addr,
            0,
            ptr::null(),
            &mut size,
            &mut alive as *mut u32 as *mut c_void,
        ) == 0
            && alive != 0
    }

    /// scope 下设备是否具备可用的流（输入 scope 看输入流、输出 scope 看输出流）。
    unsafe fn has_streams(device: AudioObjectID, scope: u32) -> bool {
        let mut size: u32 = 0;
        let addr = coreaudio_sys::AudioObjectPropertyAddress {
            mSelector: kAudioDevicePropertyStreams,
            mScope: scope,
            mElement: kAudioObjectPropertyElementMain,
        };
        if coreaudio_sys::AudioObjectGetPropertyDataSize(
            device,
            &addr,
            0,
            ptr::null(),
            &mut size,
        ) != 0
        {
            // 属性不可读时按「设备存活且有该 scope 能力」猜测 —— 走全局 scope 的设备
            // 常见于老驱动；为避免误判直接视为具备（由 is_alive 兜底）。
            return true;
        }
        let count = size as usize / size_of::<AudioObjectID>();
        if count == 0 {
            return false;
        }
        let mut ids: Vec<AudioObjectID> = vec![0; count];
        coreaudio_sys::AudioObjectGetPropertyData(
            device,
            &addr,
            0,
            ptr::null(),
            &mut size,
            ids.as_mut_ptr() as *mut c_void,
        ) == 0
    }

    unsafe fn get_default_device(scope: u32) -> Option<AudioObjectID> {
        let mut device: AudioObjectID = 0;
        let mut size = size_of::<AudioObjectID>() as u32;
        let selector = if scope == kAudioObjectPropertyScopeInput {
            kAudioHardwarePropertyDefaultInputDevice
        } else {
            kAudioHardwarePropertyDefaultOutputDevice
        };
        let addr = coreaudio_sys::AudioObjectPropertyAddress {
            mSelector: selector,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain,
        };
        if coreaudio_sys::AudioObjectGetPropertyData(
            kAudioObjectSystemObject,
            &addr,
            0,
            ptr::null(),
            &mut size,
            &mut device as *mut AudioObjectID as *mut c_void,
        ) == 0
            && device != 0
        {
            Some(device)
        } else {
            None
        }
    }

    fn transport_suffix(device: AudioObjectID) -> String {
        unsafe {
            let mut t: u32 = 0;
            let mut size = size_of::<u32>() as u32;
            let addr = coreaudio_sys::AudioObjectPropertyAddress {
                mSelector: kAudioDevicePropertyTransportType,
                mScope: kAudioObjectPropertyScopeGlobal,
                mElement: kAudioObjectPropertyElementMain,
            };
            if coreaudio_sys::AudioObjectGetPropertyData(
                device,
                &addr,
                0,
                ptr::null(),
                &mut size,
                &mut t as *mut u32 as *mut c_void,
            ) != 0
            {
                return String::new();
            }
            match t {
                coreaudio_sys::kAudioDeviceTransportTypeBuiltIn => "内置".to_string(),
                coreaudio_sys::kAudioDeviceTransportTypeUSB => "USB".to_string(),
                coreaudio_sys::kAudioDeviceTransportTypeBluetooth => "蓝牙".to_string(),
                coreaudio_sys::kAudioDeviceTransportTypeHDMI => "HDMI".to_string(),
                coreaudio_sys::kAudioDeviceTransportTypeDisplayPort => "DisplayPort".to_string(),
                coreaudio_sys::kAudioDeviceTransportTypeAirPlay => "AirPlay".to_string(),
                coreaudio_sys::kAudioDeviceTransportTypeVirtual => "虚拟".to_string(),
                _ => String::new(),
            }
        }
    }

    /// 枚举系统音频设备（scope = kAudioObjectPropertyScopeInput / Output）。
    pub(crate) unsafe fn list_devices(scope: u32) -> Vec<AudioDevice> {
        let mut size: u32 = 0;
        let addr = coreaudio_sys::AudioObjectPropertyAddress {
            mSelector: kAudioHardwarePropertyDevices,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain,
        };
        if coreaudio_sys::AudioObjectGetPropertyDataSize(
            kAudioObjectSystemObject,
            &addr,
            0,
            ptr::null(),
            &mut size,
        ) != 0
        {
            return vec![];
        }
        let count = size as usize / size_of::<AudioObjectID>();
        if count == 0 || count > 128 {
            return vec![];
        }
        let mut ids: Vec<AudioObjectID> = vec![0; count];
        if coreaudio_sys::AudioObjectGetPropertyData(
            kAudioObjectSystemObject,
            &addr,
            0,
            ptr::null(),
            &mut size,
            ids.as_mut_ptr() as *mut c_void,
        ) != 0
        {
            return vec![];
        }
        let default_dev = get_default_device(scope);
        let mut out = Vec::new();
        for id in ids {
            if id == 0 || !device_alive(id) || !has_streams(id, scope) {
                continue;
            }
            let uid = get_device_string(id, kAudioDevicePropertyDeviceUID, kAudioObjectPropertyScopeGlobal)
                .unwrap_or_default();
            if uid.is_empty() {
                continue;
            }
            // 名称优先从设备名属性取；读不到再用全局 scope 的 UID 兜底展示。
            let name = get_device_string(id, coreaudio_sys::kAudioObjectPropertyName, scope)
                .or_else(|| get_device_string(id, coreaudio_sys::kAudioObjectPropertyName, kAudioObjectPropertyScopeGlobal))
                .unwrap_or_default();
            let transport = transport_suffix(id);
            let display = if transport.is_empty() {
                if name.is_empty() { uid.clone() } else { name }
            } else {
                format!("{}（{}）", name, transport)
            };
            out.push(AudioDevice {
                id: uid,
                name: display,
                is_default: default_dev == Some(id),
            });
        }
        out
    }

    /// 按 UID 反查设备 ID。
    unsafe fn device_id_for_uid(uid: &str) -> Option<AudioObjectID> {
        let mut size: u32 = 0;
        let addr = coreaudio_sys::AudioObjectPropertyAddress {
            mSelector: kAudioHardwarePropertyDevices,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain,
        };
        if coreaudio_sys::AudioObjectGetPropertyDataSize(
            kAudioObjectSystemObject,
            &addr,
            0,
            ptr::null(),
            &mut size,
        ) != 0
        {
            return None;
        }
        let count = size as usize / size_of::<AudioObjectID>();
        let mut ids: Vec<AudioObjectID> = vec![0; count];
        if coreaudio_sys::AudioObjectGetPropertyData(
            kAudioObjectSystemObject,
            &addr,
            0,
            ptr::null(),
            &mut size,
            ids.as_mut_ptr() as *mut c_void,
        ) != 0
        {
            return None;
        }
        for id in ids {
            if let Some(u) = get_device_string(id, kAudioDevicePropertyDeviceUID, kAudioObjectPropertyScopeGlobal) {
                if u == uid {
                    return Some(id);
                }
            }
        }
        None
    }

    /// 把系统默认输出设备设为 uid 对应的设备。
    pub(crate) unsafe fn set_default_output(uid: &str) -> Result<String, String> {
        let device = device_id_for_uid(uid).ok_or_else(|| "output device not found".to_string())?;
        let addr = coreaudio_sys::AudioObjectPropertyAddress {
            mSelector: kAudioHardwarePropertyDefaultOutputDevice,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain,
        };
        let status = coreaudio_sys::AudioObjectSetPropertyData(
            kAudioObjectSystemObject,
            &addr,
            0,
            ptr::null(),
            size_of::<AudioObjectID>() as u32,
            &device as *const AudioObjectID as *const c_void,
        );
        if status != 0 {
            return Err(format!("coreaudio set default output failed: {status}"));
        }
        Ok("ok".to_string())
    }
}

/// 枚举音频设备（kind: "input" | "output"）。
#[tauri::command]
pub fn list_audio_devices(kind: String) -> Vec<AudioDevice> {
    #[cfg(target_os = "macos")]
    unsafe {
        if kind == "input" {
            return macos::list_devices(coreaudio_sys::kAudioObjectPropertyScopeInput);
        } else if kind == "output" {
            return macos::list_devices(coreaudio_sys::kAudioObjectPropertyScopeOutput);
        }
    }
    vec![]
}

/// 把系统默认输出设备切到指定 deviceId（macOS；其它平台由前端回退到 Web）。
#[tauri::command]
pub fn set_system_output(device_id: String) -> Result<String, String> {
    #[cfg(target_os = "macos")]
    unsafe {
        return macos::set_default_output(&device_id);
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = device_id;
        Err("set_system_output is only supported on macOS".to_string())
    }
}

#[cfg(all(test, target_os = "macos"))]
mod tests {
    use super::list_audio_devices;

    /// 回归：CoreAudio 枚举必须能安全返回（曾因错误解引用 CFString 属性导致
    /// SIGSEGV —— 设置页打开即闪退）。真机有音频设备，走到真实 CoreAudio 路径。
    #[test]
    fn enumerate_input_output_devices_does_not_crash() {
        let input = list_audio_devices("input".to_string());
        let output = list_audio_devices("output".to_string());
        // 设备可能有 0..N 个，关键是枚举过程不崩溃、字段可序列化。
        for d in input.iter().chain(output.iter()) {
            assert!(!d.id.is_empty());
            assert!(!d.name.is_empty());
        }
        let ids = input.iter().map(|d| d.id.as_str()).collect::<Vec<_>>();
        let mut uniq = ids.clone();
        uniq.dedup();
        assert_eq!(ids.len(), uniq.len(), "device uid 不应重复");
    }
}
