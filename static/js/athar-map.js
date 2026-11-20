(() => {
    if (customElements.get('athar-map')) return;

    class AtharMap extends HTMLElement {
        static observedAttributes = ['src'];

        constructor() {
            super();
            this.frame = document.createElement('iframe');
            this.frame.title = 'خريطة الشركة | أثر';
            this.frame.referrerPolicy = 'no-referrer';
            this.frame.loading = 'lazy';
            Object.assign(this.frame.style, {
                display: 'block', width: '100%', height: 'var(--athar-map-height, 520px)', border: '0',
            });
            this.attachShadow({ mode: 'open' });
        }

        connectedCallback() { this.render(); }
        attributeChangedCallback() { if (this.isConnected) this.render(); }

        render() {
            try {
                const source = new URL(this.getAttribute('src') || '', document.baseURI);
                if (!['http:', 'https:'].includes(source.protocol) || source.username || source.password
                    || !source.pathname.endsWith('/embed/map') || !source.searchParams.get('token')) {
                    throw new TypeError('Expected an Athar embed URL');
                }
                if (this.frame.src !== source.href) this.frame.src = source.href;
                if (!this.frame.isConnected) this.shadowRoot.append(this.frame);
            } catch {
                this.frame.remove();
                this.frame.removeAttribute('src');
            }
        }
    }
    customElements.define('athar-map', AtharMap);
})();
