document.documentElement.dataset.theme = localStorage.getItem('decision-theme') === 'dark' ? 'dark' : 'light';

const form = document.querySelector('#user-form');
const error = document.querySelector('#user-error');
const success = document.querySelector('#user-success');
const save = form.querySelector('button');
const { account, username, role, password } = form.elements;
const grants = form.querySelector('fieldset');
let users = [];
let csrf;

async function call(path, options) {
    const response = await fetch('/api' + path, options);
    if (response.status === 401) location.replace('/login');
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail.msg_ar);
    return result;
}

function syncRole() {
    grants.hidden = role.value === 'admin';
}

function selectAccount() {
    const selected = users.find(user => String(user.id) === account.value);
    username.value = selected?.username ?? '';
    username.readOnly = !!selected;
    role.value = selected?.role ?? 'viewer';
    password.value = '';
    password.required = !selected;
    document.querySelector('#password-hint').textContent = selected ? '· سيبها فاضية لو هتفضل زي ما هي' : '';
    for (const input of grants.querySelectorAll('input')) {
        input.checked = selected?.company_ids.includes(Number(input.value)) ?? false;
    }
    syncRole();
}

function showError(reason) {
    error.textContent = reason instanceof TypeError ? 'تعذّر الاتصال بالخادم.' : reason.message;
    error.hidden = false;
}

async function refreshAccounts(active = '') {
    users = await call('/users');
    account.replaceChildren(new Option('مستخدم جديد', ''));
    for (const user of users) account.append(new Option(user.username, user.id));
    account.value = active;
    selectAccount();
}

async function load() {
    try {
        const [principal, companies] = await Promise.all([call('/auth/me'), call('/companies')]);
        csrf = principal.csrf_token;
        for (const company of companies) {
            const label = document.createElement('label');
            const input = document.createElement('input');
            input.type = 'checkbox';
            input.value = company.id;
            label.append(input, company.name);
            document.querySelector('#company-options').append(label);
        }
        await refreshAccounts();
        form.hidden = false;
    } catch (reason) {
        showError(reason);
    }
}

account.addEventListener('change', selectAccount);
role.addEventListener('change', syncRole);
form.addEventListener('submit', async event => {
    event.preventDefault();
    if (save.disabled) return;
    error.hidden = success.hidden = true;
    save.disabled = true;
    const editing = account.value;
    const body = { role: role.value, company_ids: grants.hidden ? []
        : Array.from(grants.querySelectorAll('input:checked'), input => Number(input.value)) };
    if (!editing) body.username = username.value;
    if (password.value) body.password = password.value;
    try {
        const user = await call('/users' + (editing ? '/' + editing : ''), {
            method: editing ? 'PUT' : 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
            body: JSON.stringify(body),
        });
        await refreshAccounts(String(user.id));
        success.textContent = 'اتحفظ الحساب والصلاحيات.';
        success.hidden = false;
    } catch (reason) {
        showError(reason);
    } finally {
        password.value = '';
        save.disabled = false;
    }
});
load();
