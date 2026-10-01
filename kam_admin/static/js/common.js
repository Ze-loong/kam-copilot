// 公共JavaScript函数

// 所有动态文本进入 innerHTML/jQuery.html() 前都必须调用本函数。
function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>'"]/g, function(char) {
        return {
            '&': '&amp;',
            '<': '&lt;',
            '>': '&gt;',
            "'": '&#39;',
            '"': '&quot;'
        }[char];
    });
}

function safeAlertType(type) {
    const allowed = new Set(['primary', 'secondary', 'success', 'danger', 'warning', 'info']);
    return allowed.has(type) ? type : 'info';
}

// 显示提示信息
function showAlert(message, type = 'info', duration = 5000) {
    const alertId = 'alert-' + Date.now();
    const safeType = safeAlertType(type);
    const alert = `
        <div id="${alertId}" class="alert alert-${safeType} alert-dismissible fade show" role="alert">
            ${escapeHtml(message)}
            <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
        </div>
    `;
    
    // 添加到页面顶部
    $('.container-fluid').prepend(alert);
    
    // 自动消失
    if (duration > 0) {
        setTimeout(() => {
            $(`#${alertId}`).fadeOut(() => {
                $(`#${alertId}`).remove();
            });
        }, duration);
    }
}

// 显示加载状态
function showLoading(element, text = '加载中...') {
    const safeText = escapeHtml(text);
    const loadingHtml = `
        <div class="text-center">
            <div class="spinner-border text-primary" role="status">
                <span class="visually-hidden">${safeText}</span>
            </div>
            <div class="mt-2">${safeText}</div>
        </div>
    `;
    $(element).html(loadingHtml);
}

// 隐藏加载状态
function hideLoading(element) {
    $(element).empty();
}

// 格式化日期
function formatDate(dateStr, format = 'YYYY-MM-DD HH:mm:ss') {
    if (!dateStr) return '-';
    
    try {
        const date = new Date(dateStr);
        const year = date.getFullYear();
        const month = String(date.getMonth() + 1).padStart(2, '0');
        const day = String(date.getDate()).padStart(2, '0');
        const hours = String(date.getHours()).padStart(2, '0');
        const minutes = String(date.getMinutes()).padStart(2, '0');
        const seconds = String(date.getSeconds()).padStart(2, '0');
        
        return format
            .replace('YYYY', year)
            .replace('MM', month)
            .replace('DD', day)
            .replace('HH', hours)
            .replace('mm', minutes)
            .replace('ss', seconds);
    } catch (e) {
        return dateStr;
    }
}

// 格式化金额
function formatMoney(amount, currency = '¥') {
    if (amount === null || amount === undefined) return '-';
    return currency + parseFloat(amount).toFixed(2);
}

// 复制到剪贴板
function copyToClipboard(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(() => {
            showAlert('已复制到剪贴板', 'success', 2000);
        }).catch(() => {
            showAlert('复制失败', 'danger', 2000);
        });
    } else {
        // 兼容性处理
        const textArea = document.createElement('textarea');
        textArea.value = text;
        document.body.appendChild(textArea);
        textArea.select();
        try {
            document.execCommand('copy');
            showAlert('已复制到剪贴板', 'success', 2000);
        } catch (err) {
            showAlert('复制失败', 'danger', 2000);
        }
        document.body.removeChild(textArea);
    }
}

// 确认对话框
function confirmDialog(message, callback) {
    if (confirm(message)) {
        callback();
    }
}

// 处理AJAX错误
function handleAjaxError(xhr, status, error) {
    let message = '请求失败';
    
    if (xhr.responseJSON && xhr.responseJSON.error) {
        message = xhr.responseJSON.error;
    } else if (xhr.status === 401) {
        message = '未授权，请重新登录';
        setTimeout(() => {
            window.location.href = '/login';
        }, 2000);
    } else if (xhr.status === 403) {
        message = '权限不足';
    } else if (xhr.status === 404) {
        message = '资源不存在';
    } else if (xhr.status === 500) {
        message = '服务器内部错误';
    } else if (status === 'timeout') {
        message = '请求超时';
    } else if (status === 'error') {
        message = '网络错误';
    }
    
    showAlert(message, 'danger');
}

// 设置AJAX全局配置
$.ajaxSetup({
    timeout: 30000, // 30秒超时
    error: handleAjaxError
});

// 表格工具函数
const TableUtils = {
    // 创建空行
    createEmptyRow: function(colspan, message = '暂无数据') {
        return `<tr><td colspan="${Number(colspan)}" class="text-center text-muted">${escapeHtml(message)}</td></tr>`;
    },
    
    // 创建加载行
    createLoadingRow: function(colspan, message = '加载中...') {
        return `
            <tr>
                <td colspan="${colspan}" class="text-center">
                    <div class="spinner-border text-primary spinner-border-sm" role="status">
                        <span class="visually-hidden">${escapeHtml(message)}</span>
                    </div>
                    <span class="ms-2">${escapeHtml(message)}</span>
                </td>
            </tr>
        `;
    },
    
    // 创建状态徽章
    createStatusBadge: function(status, statusMap = {}) {
        const defaultMap = {
            'success': 'success',
            'active': 'success',
            'completed': 'success',
            'pending': 'warning',
            'processing': 'info',
            'failed': 'danger',
            'error': 'danger',
            'cancelled': 'secondary',
            'disabled': 'secondary'
        };
        
        const map = Object.assign(defaultMap, statusMap);
        const candidateClass = map[status] || 'secondary';
        const badgeClass = safeAlertType(candidateClass);
        
        return `<span class="badge bg-${badgeClass}">${escapeHtml(status)}</span>`;
    }
};

// 模态框工具函数
const ModalUtils = {
    // 显示确认模态框
    showConfirm: function(title, message, callback) {
        const modalId = 'confirm-modal-' + Date.now();
        const modalHtml = `
            <div class="modal fade" id="${modalId}" tabindex="-1">
                <div class="modal-dialog">
                    <div class="modal-content">
                        <div class="modal-header">
                            <h5 class="modal-title">${escapeHtml(title)}</h5>
                            <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
                        </div>
                        <div class="modal-body">
                            ${escapeHtml(message)}
                        </div>
                        <div class="modal-footer">
                            <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">取消</button>
                            <button type="button" class="btn btn-primary" id="${modalId}-confirm">确认</button>
                        </div>
                    </div>
                </div>
            </div>
        `;
        
        $('body').append(modalHtml);
        
        $(`#${modalId}`).modal('show');
        
        $(`#${modalId}-confirm`).on('click', function() {
            $(`#${modalId}`).modal('hide');
            callback();
        });
        
        $(`#${modalId}`).on('hidden.bs.modal', function() {
            $(`#${modalId}`).remove();
        });
    }
};

// 页面加载完成后的初始化
$(document).ready(function() {
    // 初始化工具提示
    const tooltipTriggerList = [].slice.call(document.querySelectorAll('[data-bs-toggle="tooltip"]'));
    tooltipTriggerList.map(function(tooltipTriggerEl) {
        return new bootstrap.Tooltip(tooltipTriggerEl);
    });
    
    // 初始化气泡提示
    const popoverTriggerList = [].slice.call(document.querySelectorAll('[data-bs-toggle="popover"]'));
    popoverTriggerList.map(function(popoverTriggerEl) {
        return new bootstrap.Popover(popoverTriggerEl);
    });
    
    // 自动关闭提示信息
    setTimeout(() => {
        $('.alert').fadeOut();
    }, 5000);
    
    // 代码块点击复制
    $(document).on('click', 'code', function() {
        const text = $(this).text();
        copyToClipboard(text);
    });
    
    // 表格行点击高亮
    $(document).on('click', 'table tbody tr', function() {
        $(this).siblings().removeClass('table-active');
        $(this).addClass('table-active');
    });
});

// 导出全局函数
window.showAlert = showAlert;
window.escapeHtml = escapeHtml;
window.showLoading = showLoading;
window.hideLoading = hideLoading;
window.formatDate = formatDate;
window.formatMoney = formatMoney;
window.copyToClipboard = copyToClipboard;
window.confirmDialog = confirmDialog;
window.TableUtils = TableUtils;
window.ModalUtils = ModalUtils; 
