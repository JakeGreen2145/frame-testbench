#pragma once
#include "openvr_driver.h"
#include <cstdlib>
#include <cstring>
#include <cstdint>
#include <limits>
#include <mutex>
#include <sstream>
#include <string>
#include <vector>

namespace frame {
// Opt-in observation only. No SDK writes, device selection, or input overrides.
class ControllerTrace {
    static constexpr size_t max_devices=4, max_components=32, max_pending=8, max_text=96;
    struct Component {
        std::string path;
        vr::VRInputComponentHandle_t handle{};
        bool boolean{};
        int scalar_type{}, scalar_units{}, create_error{}, update_error{};
        uint64_t creates{}, updates{}, activation_creates{};
    };
    struct Device {
        uint64_t id{}, generation{}, activates{}, deactivates{}, get_poses{}, publications{};
        uint32_t index=vr::k_unTrackedDeviceIndexInvalid;
        vr::PropertyContainerHandle_t container{};
        bool registered{}, active{}, activating{}, connected{}, valid{};
        int activation_error=-1, profile_error=-1, type_error=-1, role_error=-1, role{};
        std::string serial, profile, type;
        std::vector<Component> components;
    };
    struct Pending { vr::PropertyContainerHandle_t container{}; std::vector<Component> components; };
    const bool enabled_;
    std::mutex mutex_;
    std::vector<Device> devices_;
    std::vector<Pending> pending_;
    uint64_t next_id_{}, next_generation_{}, dropped_devices_{}, dropped_components_{}, dropped_pending_{}, unmatched_updates_{}, bad_pose_sizes_{};
    uint64_t host_requests_{}, input_requests_{}, property_requests_{}, alias_passthrough_{};
    static bool configured() { const char* flag=std::getenv("FRAME_TESTBENCH_TRACE_CONTROLLERS"); return flag && !std::strcmp(flag,"1"); }
    static void increment(uint64_t& value) { if(value!=std::numeric_limits<uint64_t>::max()) ++value; }
    static std::string bounded(const char* value) { return value ? std::string(value,strnlen(value,max_text)) : std::string(); }
    static std::string json(const std::string& value) {
        std::ostringstream out; out << '"';
        const char hex[]="0123456789abcdef";
        for(unsigned char c:value) {
            if(c=='"' || c=='\\') out << '\\' << char(c);
            else if(c<32 || c>=127) out << "\\u00" << hex[c>>4] << hex[c&15];
            else out << char(c);
        }
        out << '"'; return out.str();
    }
    Device* find(uint64_t id) { for(auto& d:devices_) if(d.id==id) return &d; return nullptr; }
    void add(std::vector<Component>& components, const char* path, vr::VRInputComponentHandle_t handle, bool boolean, int type, int units, int error,bool activating=false) {
        const auto name=bounded(path);
        for(auto& c:components) if(c.path==name && c.handle==handle && c.boolean==boolean) {
            increment(c.creates); if(activating) increment(c.activation_creates); c.create_error=error; return;
        }
        if(components.size()>=max_components) { increment(dropped_components_); return; }
        Component c; c.path=name; c.handle=handle; c.boolean=boolean; c.scalar_type=type; c.scalar_units=units; c.create_error=error; c.creates=1; c.activation_creates=activating ? 1 : 0;
        components.push_back(std::move(c));
    }
public:
    ControllerTrace():enabled_(configured()) {}
    bool enabled() const { return enabled_; }
    void reset() {
        if(!enabled_) return;
        std::lock_guard<std::mutex> lock(mutex_); devices_.clear(); pending_.clear();
        next_id_=next_generation_=dropped_devices_=dropped_components_=dropped_pending_=unmatched_updates_=bad_pose_sizes_=0;
        host_requests_=input_requests_=property_requests_=alias_passthrough_=0;
    }
    uint64_t added(const char* serial) {
        if(!enabled_) return 0;
        std::lock_guard<std::mutex> lock(mutex_);
        if(devices_.size()>=max_devices) { increment(dropped_devices_); return 0; }
        Device d; d.id=++next_id_; d.serial=bounded(serial); devices_.push_back(std::move(d)); return next_id_;
    }
    void registered(uint64_t id,bool accepted) {
        std::lock_guard<std::mutex> lock(mutex_); if(auto* d=find(id)) d->registered=accepted;
    }
    void activating(uint64_t id,uint32_t index,vr::PropertyContainerHandle_t container) {
        std::lock_guard<std::mutex> lock(mutex_); auto* d=find(id); if(!d) return;
        d->generation=++next_generation_; increment(d->activates); d->index=index; d->container=container;
        d->activating=true; d->active=false; d->activation_error=-1; d->components.clear();
        for(auto it=pending_.begin();it!=pending_.end();++it) if(it->container==container) {
            d->components=std::move(it->components); pending_.erase(it); break;
        }
    }
    void activated(uint64_t id,int error) {
        std::lock_guard<std::mutex> lock(mutex_); if(auto* d=find(id)) { d->activating=false; d->activation_error=error; d->active=error==vr::VRInitError_None; }
    }
    void deactivated(uint64_t id) {
        std::lock_guard<std::mutex> lock(mutex_); if(auto* d=find(id)) {
            increment(d->deactivates); d->active=d->activating=false;
            // Keep the diagnostic generation, but do not associate later index publications.
        }
    }
    void get_pose(uint64_t id,const vr::DriverPose_t& pose) {
        std::lock_guard<std::mutex> lock(mutex_); if(auto* d=find(id)) { increment(d->get_poses); d->connected=pose.deviceIsConnected; d->valid=pose.poseIsValid; }
    }
    void publication(uint32_t index,const vr::DriverPose_t& pose,uint32_t size) {
        if(!enabled_) return;
        std::lock_guard<std::mutex> lock(mutex_);
        if(size!=sizeof(pose)) { increment(bad_pose_sizes_); return; }
        for(auto& d:devices_) if((d.active || d.activating) && d.index==index) {
            increment(d.publications); d.connected=pose.deviceIsConnected; d.valid=pose.poseIsValid; return;
        }
    }
    void component(vr::PropertyContainerHandle_t container,const char* path,vr::VRInputComponentHandle_t handle,bool boolean,int type,int units,int error) {
        if(!enabled_) return;
        std::lock_guard<std::mutex> lock(mutex_);
        for(auto& d:devices_) if((d.active || d.activating) && d.container==container) { add(d.components,path,handle,boolean,type,units,error,d.activating); return; }
        for(auto& p:pending_) if(p.container==container) { add(p.components,path,handle,boolean,type,units,error); return; }
        if(pending_.size()>=max_pending) { increment(dropped_pending_); return; }
        Pending p; p.container=container; add(p.components,path,handle,boolean,type,units,error); pending_.push_back(std::move(p));
    }
    void update(vr::VRInputComponentHandle_t handle,bool boolean,int error) {
        if(!enabled_) return;
        std::lock_guard<std::mutex> lock(mutex_);
        if(handle!=vr::k_ulInvalidInputComponentHandle) {
            for(auto& d:devices_) if(d.active || d.activating) for(auto& c:d.components) if(c.handle==handle && c.boolean==boolean) { increment(c.updates); c.update_error=error; return; }
            for(auto& p:pending_) for(auto& c:p.components) if(c.handle==handle && c.boolean==boolean) { increment(c.updates); c.update_error=error; return; }
        }
        increment(unmatched_updates_);
    }
    void interface_request(const char* name,bool alias) {
        if(!enabled_ || !name) return;
        std::lock_guard<std::mutex> lock(mutex_);
        if(!std::strcmp(name,vr::IVRServerDriverHost_Version)) increment(host_requests_);
        else if(!std::strcmp(name,vr::IVRDriverInput_Version)) increment(input_requests_);
        else if(!std::strcmp(name,vr::IVRProperties_Version)) increment(property_requests_);
        if(alias) increment(alias_passthrough_);
    }
    void refresh(vr::IVRProperties* properties) {
        if(!enabled_ || !properties) return;
        // Never hold a trace lock across runtime property queries or callbacks.
        std::vector<Device> snapshot;
        { std::lock_guard<std::mutex> lock(mutex_); snapshot=devices_; }
        vr::CVRPropertyHelpers props(properties);
        for(const auto& copy:snapshot) if(copy.active && copy.container) {
            char profile[max_text+1]{}, type[max_text+1]{};
            vr::ETrackedPropertyError profile_error=vr::TrackedProp_Success, type_error=vr::TrackedProp_Success, role_error=vr::TrackedProp_Success;
            props.GetStringProperty(copy.container,vr::Prop_InputProfilePath_String,profile,sizeof(profile),&profile_error);
            props.GetStringProperty(copy.container,vr::Prop_ControllerType_String,type,sizeof(type),&type_error);
            int role=props.GetInt32Property(copy.container,vr::Prop_ControllerRoleHint_Int32,&role_error);
            std::lock_guard<std::mutex> lock(mutex_); auto* d=find(copy.id);
            if(d && d->active && d->generation==copy.generation && d->container==copy.container) {
                d->profile=bounded(profile); d->type=bounded(type); d->role=role;
                d->profile_error=profile_error; d->type_error=type_error; d->role_error=role_error;
            }
        }
    }
    std::string status() {
        std::lock_guard<std::mutex> lock(mutex_); std::ostringstream out;
        out << std::boolalpha << "{\"ok\":true,\"schema_version\":1,\"enabled\":" << enabled_
            << ",\"max_devices\":" << max_devices << ",\"max_components_per_container\":" << max_components << ",\"max_pending_containers\":" << max_pending
            << ",\"dropped_devices\":" << dropped_devices_ << ",\"dropped_components\":" << dropped_components_ << ",\"dropped_pending_containers\":" << dropped_pending_
            << ",\"pending_containers\":" << pending_.size() << ",\"unmatched_updates\":" << unmatched_updates_ << ",\"bad_pose_sizes\":" << bad_pose_sizes_
            << ",\"host_interface_requests\":" << host_requests_ << ",\"input_interface_requests\":" << input_requests_ << ",\"property_interface_requests\":" << property_requests_
            << ",\"alias_passthrough\":" << alias_passthrough_ << ",\"devices\":[";
        bool first=true;
        for(const auto& d:devices_) {
            if(!first) out << ',';
            first=false;
            out << "{\"id\":" << d.id << ",\"generation\":" << d.generation << ",\"serial_diagnostic\":" << json(d.serial)
                << ",\"registered\":" << d.registered << ",\"active\":" << d.active << ",\"activating\":" << d.activating << ",\"index\":" << d.index << ",\"container\":" << d.container
                << ",\"activation_error\":" << d.activation_error << ",\"activations\":" << d.activates << ",\"deactivations\":" << d.deactivates << ",\"get_pose_calls\":" << d.get_poses << ",\"pose_publications\":" << d.publications
                << ",\"last_pose_connected\":" << d.connected << ",\"last_pose_valid\":" << d.valid
                << ",\"profile\":" << json(d.profile) << ",\"controller_type\":" << json(d.type) << ",\"role_hint\":" << d.role
                << ",\"profile_error\":" << d.profile_error << ",\"type_error\":" << d.type_error << ",\"role_error\":" << d.role_error << ",\"components\":[";
            bool first_component=true;
            for(const auto& c:d.components) {
                if(!first_component) out << ',';
                first_component=false;
                out << "{\"path\":" << json(c.path) << ",\"handle\":" << c.handle << ",\"boolean\":" << c.boolean << ",\"scalar_type\":" << c.scalar_type << ",\"scalar_units\":" << c.scalar_units
                    << ",\"create_error\":" << c.create_error << ",\"creates\":" << c.creates << ",\"creates_during_activation\":" << c.activation_creates << ",\"updates\":" << c.updates << ",\"last_update_error\":" << c.update_error << '}';
            }
            out << "]}";
        }
        out << "]}\n";
        const auto result=out.str();
        if(result.size()>60000) return "{\"ok\":true,\"schema_version\":1,\"enabled\":true,\"response_truncated\":true,\"devices\":[],\"error\":\"bounded trace reply exceeded 60000 bytes\"}\n";
        return result;
    }
};
} // namespace frame
