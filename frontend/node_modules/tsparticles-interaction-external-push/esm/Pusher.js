var __classPrivateFieldSet = (this && this.__classPrivateFieldSet) || function (receiver, state, value, kind, f) {
    if (kind === "m") throw new TypeError("Private method is not writable");
    if (kind === "a" && !f) throw new TypeError("Private accessor was defined without a setter");
    if (typeof state === "function" ? receiver !== state || !f : !state.has(receiver)) throw new TypeError("Cannot write private member to an object whose class did not declare it");
    return (kind === "a" ? f.call(receiver, value) : f ? f.value = value : state.set(receiver, value)), value;
};
var __classPrivateFieldGet = (this && this.__classPrivateFieldGet) || function (receiver, state, kind, f) {
    if (kind === "a" && !f) throw new TypeError("Private accessor was defined without a getter");
    if (typeof state === "function" ? receiver !== state || !f : !state.has(receiver)) throw new TypeError("Cannot read private member from an object whose class did not declare it");
    return kind === "m" ? f : kind === "a" ? f.call(receiver) : f ? f.value : state.get(receiver);
};
var _Pusher_container;
import { ExternalInteractorBase } from "tsparticles-engine";
import { Push } from "./Options/Classes/Push";
import { itemFromArray } from "tsparticles-engine";
export class Pusher extends ExternalInteractorBase {
    constructor(container) {
        super(container);
        _Pusher_container.set(this, void 0);
        __classPrivateFieldSet(this, _Pusher_container, container, "f");
        this.handleClickMode = (mode) => {
            if (mode !== "push") {
                return;
            }
            const container = __classPrivateFieldGet(this, _Pusher_container, "f"), options = container.actualOptions, pushOptions = options.interactivity.modes.push;
            if (!pushOptions) {
                return;
            }
            const pushNb = pushOptions.quantity;
            if (pushNb <= 0) {
                return;
            }
            const group = itemFromArray([undefined, ...pushOptions.groups]), groupOptions = group !== undefined ? container.actualOptions.particles.groups[group] : undefined;
            container.particles.push(pushNb, container.interactivity.mouse, groupOptions, group);
        };
    }
    clear() {
    }
    init() {
    }
    async interact() {
    }
    isEnabled() {
        return true;
    }
    loadModeOptions(options, ...sources) {
        if (!options.push) {
            options.push = new Push();
        }
        for (const source of sources) {
            options.push.load(source === null || source === void 0 ? void 0 : source.push);
        }
    }
    reset() {
    }
}
_Pusher_container = new WeakMap();
