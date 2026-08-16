import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach } from 'vitest';

/**
 * jsdom implements no layout, so the browser APIs a positioned popover uses are
 * simply absent. Radix measures its trigger to place the panel; without these it
 * throws on mount and the test failure names `ResizeObserver` rather than
 * anything to do with the component.
 *
 * Stubs, not polyfills: nothing here is asserted on. Position is a property of a
 * real browser and these tests are about behaviour.
 */
globalThis.ResizeObserver ??= class {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
};

// `in` rather than reading the member: `Element.prototype.x ??= …` reads an
// unbound method to test it, which is the shape `unbound-method` exists to catch.
if (!('hasPointerCapture' in Element.prototype)) {
  Object.assign(Element.prototype, {
    hasPointerCapture: () => false,
    setPointerCapture: () => undefined,
    releasePointerCapture: () => undefined,
    scrollIntoView: () => undefined,
  });
}

afterEach(() => {
  cleanup();
});
