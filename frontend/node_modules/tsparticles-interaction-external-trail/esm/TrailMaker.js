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
var _TrailMaker_container;
import { ExternalInteractorBase, isInArray } from "tsparticles-engine";
import { Trail } from "./Options/Classes/Trail";
export class TrailMaker extends ExternalInteractorBase {
    constructor(container) {
        super(container);
        _TrailMaker_container.set(this, void 0);
        __classPrivateFieldSet(this, _TrailMaker_container, container, "f");
        this.delay = 0;
    }
    clear() {
    }
    init() {
    }
    async interact(delta) {
        var _a, _b, _c, _d;
        if (!this.container.retina.reduceFactor) {
            return;
        }
        const container = __classPrivateFieldGet(this, _TrailMaker_container, "f"), options = container.actualOptions, trailOptions = options.interactivity.modes.trail;
        if (!trailOptions) {
            return;
        }
        const optDelay = (trailOptions.delay * 1000) / this.container.retina.reduceFactor;
        if (this.delay < optDelay) {
            this.delay += delta.value;
        }
        if (this.delay < optDelay) {
            return;
        }
        let canEmit = true;
        if (trailOptions.pauseOnStop) {
            if (container.interactivity.mouse.position === this.lastPosition ||
                (((_a = container.interactivity.mouse.position) === null || _a === void 0 ? void 0 : _a.x) === ((_b = this.lastPosition) === null || _b === void 0 ? void 0 : _b.x) &&
                    ((_c = container.interactivity.mouse.position) === null || _c === void 0 ? void 0 : _c.y) === ((_d = this.lastPosition) === null || _d === void 0 ? void 0 : _d.y))) {
                canEmit = false;
            }
        }
        if (container.interactivity.mouse.position) {
            this.lastPosition = {
                x: container.interactivity.mouse.position.x,
                y: container.interactivity.mouse.position.y,
            };
        }
        else {
            delete this.lastPosition;
        }
        if (canEmit) {
            container.particles.push(trailOptions.quantity, container.interactivity.mouse, trailOptions.particles);
        }
        this.delay -= optDelay;
    }
    isEnabled(particle) {
        var _a;
        const container = this.container, options = container.actualOptions, mouse = container.interactivity.mouse, events = ((_a = particle === null || particle === void 0 ? void 0 : particle.interactivity) !== null && _a !== void 0 ? _a : options.interactivity).events;
        return ((mouse.clicking && mouse.inside && !!mouse.position && isInArray("trail", events.onClick.mode)) ||
            (mouse.inside && !!mouse.position && isInArray("trail", events.onHover.mode)));
    }
    loadModeOptions(options, ...sources) {
        if (!options.trail) {
            options.trail = new Trail();
        }
        for (const source of sources) {
            options.trail.load(source === null || source === void 0 ? void 0 : source.trail);
        }
    }
    reset() {
    }
}
_TrailMaker_container = new WeakMap();
