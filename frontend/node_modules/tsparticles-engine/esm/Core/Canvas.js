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
var _Canvas_colorPlugins, _Canvas_context, _Canvas_postDrawUpdaters, _Canvas_preDrawUpdaters, _Canvas_resizePlugins;
import { clear, drawParticle, drawParticlePlugin, drawPlugin, paintBase } from "../Utils/CanvasUtils";
import { getStyleFromHsl, getStyleFromRgb, rangeColorToHsl, rangeColorToRgb } from "../Utils/ColorUtils";
import { deepExtend } from "../Utils/Utils";
import { generatedAttribute } from "./Utils/Constants";
function setTransformValue(factor, newFactor, key) {
    var _a;
    const newValue = newFactor[key];
    if (newValue !== undefined) {
        factor[key] = ((_a = factor[key]) !== null && _a !== void 0 ? _a : 1) * newValue;
    }
}
export class Canvas {
    constructor(container) {
        this.container = container;
        _Canvas_colorPlugins.set(this, void 0);
        _Canvas_context.set(this, void 0);
        _Canvas_postDrawUpdaters.set(this, void 0);
        _Canvas_preDrawUpdaters.set(this, void 0);
        _Canvas_resizePlugins.set(this, void 0);
        this.size = {
            height: 0,
            width: 0,
        };
        __classPrivateFieldSet(this, _Canvas_context, null, "f");
        this.generatedCanvas = false;
        __classPrivateFieldSet(this, _Canvas_preDrawUpdaters, [], "f");
        __classPrivateFieldSet(this, _Canvas_postDrawUpdaters, [], "f");
        __classPrivateFieldSet(this, _Canvas_resizePlugins, [], "f");
        __classPrivateFieldSet(this, _Canvas_colorPlugins, [], "f");
    }
    clear() {
        const options = this.container.actualOptions, trail = options.particles.move.trail;
        if (options.backgroundMask.enable) {
            this.paint();
        }
        else if (trail.enable && trail.length > 0 && this.trailFillColor) {
            this.paintBase(getStyleFromRgb(this.trailFillColor, 1 / trail.length));
        }
        else {
            this.draw((ctx) => {
                clear(ctx, this.size);
            });
        }
    }
    destroy() {
        var _a;
        if (this.generatedCanvas) {
            (_a = this.element) === null || _a === void 0 ? void 0 : _a.remove();
        }
        else {
            this.resetOriginalStyle();
        }
        this.draw((ctx) => {
            clear(ctx, this.size);
        });
        __classPrivateFieldSet(this, _Canvas_preDrawUpdaters, [], "f");
        __classPrivateFieldSet(this, _Canvas_postDrawUpdaters, [], "f");
        __classPrivateFieldSet(this, _Canvas_resizePlugins, [], "f");
        __classPrivateFieldSet(this, _Canvas_colorPlugins, [], "f");
    }
    draw(cb) {
        if (!__classPrivateFieldGet(this, _Canvas_context, "f")) {
            return;
        }
        return cb(__classPrivateFieldGet(this, _Canvas_context, "f"));
    }
    drawParticle(particle, delta) {
        var _a;
        if (particle.spawning || particle.destroyed) {
            return;
        }
        const radius = particle.getRadius();
        if (radius <= 0) {
            return;
        }
        const pfColor = particle.getFillColor(), psColor = (_a = particle.getStrokeColor()) !== null && _a !== void 0 ? _a : pfColor;
        let [fColor, sColor] = this.getPluginParticleColors(particle);
        if (!fColor) {
            fColor = pfColor;
        }
        if (!sColor) {
            sColor = psColor;
        }
        if (!fColor && !sColor) {
            return;
        }
        this.draw((ctx) => {
            var _a, _b, _c, _d, _e;
            const options = this.container.actualOptions, zIndexOptions = particle.options.zIndex, zOpacityFactor = (1 - particle.zIndexFactor) ** zIndexOptions.opacityRate, opacity = (_c = (_a = particle.bubble.opacity) !== null && _a !== void 0 ? _a : (_b = particle.opacity) === null || _b === void 0 ? void 0 : _b.value) !== null && _c !== void 0 ? _c : 1, strokeOpacity = (_e = (_d = particle.stroke) === null || _d === void 0 ? void 0 : _d.opacity) !== null && _e !== void 0 ? _e : opacity, zOpacity = opacity * zOpacityFactor, zStrokeOpacity = strokeOpacity * zOpacityFactor, transform = {}, colorStyles = {
                fill: fColor ? getStyleFromHsl(fColor, zOpacity) : undefined,
            };
            colorStyles.stroke = sColor ? getStyleFromHsl(sColor, zStrokeOpacity) : colorStyles.fill;
            this.applyPreDrawUpdaters(ctx, particle, radius, zOpacity, colorStyles, transform);
            drawParticle({
                container: this.container,
                context: ctx,
                particle,
                delta,
                colorStyles,
                backgroundMask: options.backgroundMask.enable,
                composite: options.backgroundMask.composite,
                radius: radius * (1 - particle.zIndexFactor) ** zIndexOptions.sizeRate,
                opacity: zOpacity,
                shadow: particle.options.shadow,
                transform,
            });
            this.applyPostDrawUpdaters(particle);
        });
    }
    drawParticlePlugin(plugin, particle, delta) {
        this.draw((ctx) => {
            drawParticlePlugin(ctx, plugin, particle, delta);
        });
    }
    drawPlugin(plugin, delta) {
        this.draw((ctx) => {
            drawPlugin(ctx, plugin, delta);
        });
    }
    init() {
        this.resize();
        this.initStyle();
        this.initCover();
        this.initTrail();
        this.initBackground();
        this.initUpdaters();
        this.initPlugins();
        this.paint();
    }
    initBackground() {
        const options = this.container.actualOptions, background = options.background, element = this.element, elementStyle = element === null || element === void 0 ? void 0 : element.style;
        if (!elementStyle) {
            return;
        }
        if (background.color) {
            const color = rangeColorToRgb(background.color);
            elementStyle.backgroundColor = color ? getStyleFromRgb(color, background.opacity) : "";
        }
        else {
            elementStyle.backgroundColor = "";
        }
        elementStyle.backgroundImage = background.image || "";
        elementStyle.backgroundPosition = background.position || "";
        elementStyle.backgroundRepeat = background.repeat || "";
        elementStyle.backgroundSize = background.size || "";
    }
    initPlugins() {
        __classPrivateFieldSet(this, _Canvas_resizePlugins, [], "f");
        for (const [, plugin] of this.container.plugins) {
            if (plugin.resize) {
                __classPrivateFieldGet(this, _Canvas_resizePlugins, "f").push(plugin);
            }
            if (plugin.particleFillColor || plugin.particleStrokeColor) {
                __classPrivateFieldGet(this, _Canvas_colorPlugins, "f").push(plugin);
            }
        }
    }
    initUpdaters() {
        __classPrivateFieldSet(this, _Canvas_preDrawUpdaters, [], "f");
        __classPrivateFieldSet(this, _Canvas_postDrawUpdaters, [], "f");
        for (const updater of this.container.particles.updaters) {
            if (updater.afterDraw) {
                __classPrivateFieldGet(this, _Canvas_postDrawUpdaters, "f").push(updater);
            }
            if (updater.getColorStyles || updater.getTransformValues || updater.beforeDraw) {
                __classPrivateFieldGet(this, _Canvas_preDrawUpdaters, "f").push(updater);
            }
        }
    }
    loadCanvas(canvas) {
        var _a;
        if (this.generatedCanvas) {
            (_a = this.element) === null || _a === void 0 ? void 0 : _a.remove();
        }
        this.generatedCanvas =
            canvas.dataset && generatedAttribute in canvas.dataset
                ? canvas.dataset[generatedAttribute] === "true"
                : this.generatedCanvas;
        this.element = canvas;
        this.originalStyle = deepExtend({}, this.element.style);
        this.size.height = canvas.offsetHeight;
        this.size.width = canvas.offsetWidth;
        __classPrivateFieldSet(this, _Canvas_context, this.element.getContext("2d"), "f");
        this.container.retina.init();
        this.initBackground();
    }
    paint() {
        const options = this.container.actualOptions;
        this.draw((ctx) => {
            if (options.backgroundMask.enable && options.backgroundMask.cover) {
                clear(ctx, this.size);
                this.paintBase(this.coverColorStyle);
            }
            else {
                this.paintBase();
            }
        });
    }
    resize() {
        if (!this.element) {
            return;
        }
        const container = this.container, pxRatio = container.retina.pixelRatio, size = container.canvas.size, newSize = {
            width: this.element.offsetWidth * pxRatio,
            height: this.element.offsetHeight * pxRatio,
        };
        if (newSize.height === size.height &&
            newSize.width === size.width &&
            newSize.height === this.element.height &&
            newSize.width === this.element.width) {
            return;
        }
        const oldSize = Object.assign({}, size);
        this.element.width = size.width = this.element.offsetWidth * pxRatio;
        this.element.height = size.height = this.element.offsetHeight * pxRatio;
        if (this.container.started) {
            this.resizeFactor = {
                width: size.width / oldSize.width,
                height: size.height / oldSize.height,
            };
        }
    }
    async windowResize() {
        if (!this.element) {
            return;
        }
        this.resize();
        const container = this.container, needsRefresh = container.updateActualOptions();
        container.particles.setDensity();
        this.applyResizePlugins();
        if (needsRefresh) {
            await container.refresh();
        }
    }
    applyPostDrawUpdaters(particle) {
        var _a;
        for (const updater of __classPrivateFieldGet(this, _Canvas_postDrawUpdaters, "f")) {
            (_a = updater.afterDraw) === null || _a === void 0 ? void 0 : _a.call(updater, particle);
        }
    }
    applyPreDrawUpdaters(ctx, particle, radius, zOpacity, colorStyles, transform) {
        var _a;
        for (const updater of __classPrivateFieldGet(this, _Canvas_preDrawUpdaters, "f")) {
            if (updater.getColorStyles) {
                const { fill, stroke } = updater.getColorStyles(particle, ctx, radius, zOpacity);
                if (fill) {
                    colorStyles.fill = fill;
                }
                if (stroke) {
                    colorStyles.stroke = stroke;
                }
            }
            if (updater.getTransformValues) {
                const updaterTransform = updater.getTransformValues(particle);
                for (const key in updaterTransform) {
                    setTransformValue(transform, updaterTransform, key);
                }
            }
            (_a = updater.beforeDraw) === null || _a === void 0 ? void 0 : _a.call(updater, particle);
        }
    }
    applyResizePlugins() {
        var _a;
        for (const plugin of __classPrivateFieldGet(this, _Canvas_resizePlugins, "f")) {
            (_a = plugin.resize) === null || _a === void 0 ? void 0 : _a.call(plugin);
        }
    }
    getPluginParticleColors(particle) {
        let fColor, sColor;
        for (const plugin of __classPrivateFieldGet(this, _Canvas_colorPlugins, "f")) {
            if (!fColor && plugin.particleFillColor) {
                fColor = rangeColorToHsl(plugin.particleFillColor(particle));
            }
            if (!sColor && plugin.particleStrokeColor) {
                sColor = rangeColorToHsl(plugin.particleStrokeColor(particle));
            }
            if (fColor && sColor) {
                break;
            }
        }
        return [fColor, sColor];
    }
    initCover() {
        const options = this.container.actualOptions, cover = options.backgroundMask.cover, color = cover.color, coverRgb = rangeColorToRgb(color);
        if (coverRgb) {
            const coverColor = {
                r: coverRgb.r,
                g: coverRgb.g,
                b: coverRgb.b,
                a: cover.opacity,
            };
            this.coverColorStyle = getStyleFromRgb(coverColor, coverColor.a);
        }
    }
    initStyle() {
        const element = this.element, options = this.container.actualOptions;
        if (!element) {
            return;
        }
        if (options.fullScreen.enable) {
            this.originalStyle = deepExtend({}, element.style);
            element.style.setProperty("position", "fixed", "important");
            element.style.setProperty("z-index", options.fullScreen.zIndex.toString(10), "important");
            element.style.setProperty("top", "0", "important");
            element.style.setProperty("left", "0", "important");
            element.style.setProperty("width", "100%", "important");
            element.style.setProperty("height", "100%", "important");
        }
        else {
            this.resetOriginalStyle();
        }
        for (const key in options.style) {
            if (!key || !options.style) {
                continue;
            }
            const value = options.style[key];
            if (!value) {
                continue;
            }
            element.style.setProperty(key, value, "important");
        }
    }
    initTrail() {
        const options = this.container.actualOptions, trail = options.particles.move.trail, fillColor = rangeColorToRgb(trail.fillColor);
        if (fillColor) {
            const trail = options.particles.move.trail;
            this.trailFillColor = {
                r: fillColor.r,
                g: fillColor.g,
                b: fillColor.b,
                a: 1 / trail.length,
            };
        }
    }
    paintBase(baseColor) {
        this.draw((ctx) => {
            paintBase(ctx, this.size, baseColor);
        });
    }
    resetOriginalStyle() {
        const element = this.element, originalStyle = this.originalStyle;
        if (element && originalStyle) {
            element.style.position = originalStyle.position;
            element.style.zIndex = originalStyle.zIndex;
            element.style.top = originalStyle.top;
            element.style.left = originalStyle.left;
            element.style.width = originalStyle.width;
            element.style.height = originalStyle.height;
        }
    }
}
_Canvas_colorPlugins = new WeakMap(), _Canvas_context = new WeakMap(), _Canvas_postDrawUpdaters = new WeakMap(), _Canvas_preDrawUpdaters = new WeakMap(), _Canvas_resizePlugins = new WeakMap();
