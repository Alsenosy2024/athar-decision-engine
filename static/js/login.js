document.documentElement.dataset.theme = localStorage.getItem('decision-theme') === 'dark' ? 'dark' : 'light';

const form = document.querySelector('#login-form');
const feedback = document.querySelector('#login-error');
const button = form.querySelector('button');

form.addEventListener('submit', async event => {
    event.preventDefault();
    if (button.disabled) return;
    button.disabled = true;
    feedback.hidden = true;
    const credentials = Object.fromEntries(new FormData(form));
    try {
        const response = await fetch('/api/auth/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Athar-Login': '1' },
            body: JSON.stringify(credentials),
        });
        const result = await response.json();
        if (!response.ok) throw new Error(result.detail.msg_ar);
        location.replace('/' + location.hash);
    } catch (error) {
        feedback.textContent = error instanceof TypeError ? 'تعذّر الاتصال بالخادم.' : error.message;
        feedback.hidden = false;
    } finally {
        form.elements.password.value = '';
        button.disabled = false;
    }
});
