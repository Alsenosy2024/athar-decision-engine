/* Keep briefing requests independent of scenario controls and tied to the visible model. */
function mapBriefing(page) {
    let latestRequest = 0;
    const briefing = {
        briefing: null, briefingLoading: false, briefingError: '', alertId: '',
        get activeAlert() { return this.briefing?.alerts.find(alert => alert.id === this.alertId); },
        get alertTrace() { return this.briefing?.traces[this.activeAlert?.node_id]; },
        alertsFor(department) {
            return this.briefing?.alerts.filter(alert => alert.department === department) || [];
        },
        resetBriefing() {
            latestRequest++;
            this.briefing = null;
            this.briefingLoading = false;
            this.briefingError = this.alertId = '';
        },
        async refreshBriefing() {
            const current = ++latestRequest;
            this.briefingLoading = true;
            this.briefingError = '';
            try {
                const query = new URLSearchParams(this.selection);
                const values = await api('/enterprise/map/briefing?' + query);
                if (current !== latestRequest) return;
                this.briefing = values;
                if (!this.activeAlert) this.alertId = '';
            } catch (error) {
                if (current === latestRequest) this.briefingError = error.message;
            } finally {
                if (current === latestRequest) this.briefingLoading = false;
            }
        },
        selectAlert(id) {
            this.alertId = id;
            if (this.activeAlert) this.focus = this.activeAlert.department;
        },
        openDepartment(department) {
            if (this.focus === department && this.activeAlert) {
                this.focus = '';
                this.selectAlert('');
                return;
            }
            const alert = this.alertsFor(department)[0];
            this.focus = this.focus === department && !alert ? '' : department;
            this.selectAlert(alert?.id ?? '');
        },
        traced(department) {
            return this.alertTrace?.path.some(edge =>
                edge.from_department === department || edge.to_department === department) || false;
        },
        traceEffects() {
            if (!this.alertTrace?.path.length) return null;
            const adverse = this.activeAlert.kind === 'threshold';
            return new Map(this.alertTrace.path.map(edge => [
                mapPair(edge.from_department, edge.to_department), { progress: 1, pulse: 0, adverse },
            ]));
        },
    };
    return Object.defineProperties(page, Object.getOwnPropertyDescriptors(briefing));
}
