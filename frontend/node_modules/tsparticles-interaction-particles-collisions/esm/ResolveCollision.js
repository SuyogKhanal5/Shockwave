import { absorb } from "./Absorb";
import { bounce } from "./Bounce";
import { destroy } from "./Destroy";
export function resolveCollision(p1, p2, fps, pixelRatio) {
    switch (p1.options.collisions.mode) {
        case "absorb": {
            absorb(p1, p2, fps, pixelRatio);
            break;
        }
        case "bounce": {
            bounce(p1, p2);
            break;
        }
        case "destroy": {
            destroy(p1, p2);
            break;
        }
    }
}
