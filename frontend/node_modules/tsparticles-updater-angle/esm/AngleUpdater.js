import { getRandom, getRangeValue } from "tsparticles-engine";
function updateAngle(particle, delta) {
    var _a, _b;
    const rotate = particle.rotate;
    if (!rotate) {
        return;
    }
    const rotateOptions = particle.options.rotate, rotateAnimation = rotateOptions.animation, speed = ((_a = rotate.velocity) !== null && _a !== void 0 ? _a : 0) * delta.factor, max = 2 * Math.PI, decay = (_b = rotate.decay) !== null && _b !== void 0 ? _b : 1;
    if (!rotateAnimation.enable) {
        return;
    }
    switch (rotate.status) {
        case 0:
            rotate.value += speed;
            if (rotate.value > max) {
                rotate.value -= max;
            }
            break;
        case 1:
        default:
            rotate.value -= speed;
            if (rotate.value < 0) {
                rotate.value += max;
            }
            break;
    }
    if (rotate.velocity && decay !== 1) {
        rotate.velocity *= decay;
    }
}
export class AngleUpdater {
    constructor(container) {
        this.container = container;
    }
    init(particle) {
        const rotateOptions = particle.options.rotate;
        particle.rotate = {
            enable: rotateOptions.animation.enable,
            value: (getRangeValue(rotateOptions.value) * Math.PI) / 180,
        };
        let rotateDirection = rotateOptions.direction;
        if (rotateDirection === "random") {
            const index = Math.floor(getRandom() * 2);
            rotateDirection = index > 0 ? "counter-clockwise" : "clockwise";
        }
        switch (rotateDirection) {
            case "counter-clockwise":
            case "counterClockwise":
                particle.rotate.status = 1;
                break;
            case "clockwise":
                particle.rotate.status = 0;
                break;
        }
        const rotateAnimation = particle.options.rotate.animation;
        if (rotateAnimation.enable) {
            particle.rotate.decay = 1 - getRangeValue(rotateAnimation.decay);
            particle.rotate.velocity =
                (getRangeValue(rotateAnimation.speed) / 360) * this.container.retina.reduceFactor;
            if (!rotateAnimation.sync) {
                particle.rotate.velocity *= getRandom();
            }
        }
        particle.rotation = particle.rotate.value;
    }
    isEnabled(particle) {
        const rotate = particle.options.rotate, rotateAnimation = rotate.animation;
        return !particle.destroyed && !particle.spawning && rotateAnimation.enable && !rotate.path;
    }
    update(particle, delta) {
        var _a, _b;
        if (!this.isEnabled(particle)) {
            return;
        }
        updateAngle(particle, delta);
        particle.rotation = (_b = (_a = particle.rotate) === null || _a === void 0 ? void 0 : _a.value) !== null && _b !== void 0 ? _b : 0;
    }
}
