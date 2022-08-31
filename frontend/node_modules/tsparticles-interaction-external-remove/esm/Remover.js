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
var _Remover_container;
import { ExternalInteractorBase } from "tsparticles-engine";
import { Remove } from "./Options/Classes/Remove";
export class Remover extends ExternalInteractorBase {
    constructor(container) {
        super(container);
        _Remover_container.set(this, void 0);
        __classPrivateFieldSet(this, _Remover_container, container, "f");
        this.handleClickMode = (mode) => {
            const container = __classPrivateFieldGet(this, _Remover_container, "f"), options = container.actualOptions;
            if (!options.interactivity.modes.remove || mode !== "remove") {
                return;
            }
            const removeNb = options.interactivity.modes.remove.quantity;
            container.particles.removeQuantity(removeNb);
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
        if (!options.remove) {
            options.remove = new Remove();
        }
        for (const source of sources) {
            options.remove.load(source === null || source === void 0 ? void 0 : source.remove);
        }
    }
    reset() {
    }
}
_Remover_container = new WeakMap();
