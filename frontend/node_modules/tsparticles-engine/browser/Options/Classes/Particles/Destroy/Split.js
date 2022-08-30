import { SplitFactor } from "./SplitFactor";
import { SplitRate } from "./SplitRate";
import { deepExtend } from "../../../../Utils/Utils";
export class Split {
    constructor() {
        this.count = 1;
        this.factor = new SplitFactor();
        this.rate = new SplitRate();
        this.sizeOffset = true;
    }
    load(data) {
        if (!data) {
            return;
        }
        if (data.count !== undefined) {
            this.count = data.count;
        }
        this.factor.load(data.factor);
        this.rate.load(data.rate);
        if (data.particles !== undefined) {
            if (data.particles instanceof Array) {
                this.particles = data.particles.map((s) => {
                    return deepExtend({}, s);
                });
            }
            else {
                this.particles = deepExtend({}, data.particles);
            }
        }
        if (data.sizeOffset !== undefined) {
            this.sizeOffset = data.sizeOffset;
        }
    }
}
