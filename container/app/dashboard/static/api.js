(function () {
    'use strict';

    window.__dashboard = window.__dashboard || {};
    window.__dashboard.rootPath = (window.__dashboard && window.__dashboard.rootPath) || '';
    window.__dashboard.loginUrl = (window.__dashboard && window.__dashboard.loginUrl) || '/dashboard/login';

    var __dashboard = window.__dashboard;

    var dashUrl = function (url) {
        if (!url || typeof url !== 'string' || url.charAt(0) !== '/') {
            return url;
        }
        var rootPath = (__dashboard && __dashboard.rootPath) || '';
        if (!rootPath) {
            return url;
        }
        return rootPath + url;
    };

    function getErrorDetail(payload, fallback) {
        if (!payload) {
            return fallback;
        }
        if (typeof payload.detail === 'string' && payload.detail) {
            return payload.detail;
        }
        if (Array.isArray(payload.detail) && payload.detail.length && payload.detail[0] && payload.detail[0].msg) {
            return payload.detail[0].msg;
        }
        if (typeof payload.message === 'string' && payload.message) {
            return payload.message;
        }
        return fallback;
    }

    function buildError(message, response, payload) {
        var error = new Error(message);
        error.status = response ? response.status : null;
        error.detail = message;
        error.payload = payload || null;
        return error;
    }

    var dashboardApi = {
        loginUrl: __dashboard.loginUrl,

        redirectToLogin: function (redirectUrl) {
            window.location.assign(redirectUrl || this.loginUrl);
        },

        async request(url, options) {
            var requestUrl = typeof url === 'string' && url.charAt(0) === '/' ? dashUrl(url) : url;
            var response = await fetch(requestUrl, Object.assign({ credentials: 'same-origin' }, options || {}));
            var redirectUrl = response.headers.get('HX-Redirect');

            if (response.status === 401 || redirectUrl) {
                var authError = buildError('Session expired', response, null);
                authError.code = 'auth_expired';
                authError.redirectTo = redirectUrl || this.loginUrl;
                this.redirectToLogin(authError.redirectTo);
                throw authError;
            }

            if (!response.ok) {
                var payload = null;
                var fallback = response.statusText || ('Request failed (' + response.status + ')');

                try {
                    payload = await response.clone().json();
                } catch (_) {
                    payload = null;
                }

                if (payload) {
                    throw buildError(getErrorDetail(payload, fallback), response, payload);
                }

                try {
                    var text = await response.text();
                    if (text) {
                        fallback = text;
                    }
                } catch (_) {
                }

                throw buildError(fallback, response, null);
            }

            return response;
        },

        async json(url, options) {
            var response = await this.request(url, options);
            return await response.json();
        },

        async safeJson(url, options) {
            try {
                return await this.json(url, options);
            } catch (e) {
                if (e.code === 'auth_expired') return null;
                console.error('API request failed: ' + url, e);
                return null;
            }
        }
    };

    var dashFetch = function (url, options) {
        return dashboardApi.request(url, options);
    };

    dashFetch.json = function (url, options) {
        return dashboardApi.json(url, options);
    };

    window.dashUrl = dashUrl;
    window.dashboardApi = dashboardApi;
    window.dashFetch = dashFetch;
})();
