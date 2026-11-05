<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api, errorMessage, type UserAccount, type Ownership } from './api'
const users = ref<UserAccount[]>([]),
  ownerships = ref<(Ownership & { version: number })[]>([]),
  error = ref(''),
  busy = ref(false)
const user = ref({
  username: '',
  display_name: '',
  password: '',
  role: 'sales',
  can_correct: false,
  active: true,
  dingtalk_userid: '',
})
const scope = ref({
  platform: '',
  shop_id: '',
  site: '',
  target_id: '',
  owner_id: '',
  supervisor_id: '',
  autonomous: false,
})
async function load() {
  busy.value = true
  try {
    const [u, o] = await Promise.all([
      api.get<UserAccount[]>('/users'),
      api.get<(Ownership & { version: number })[]>('/ownerships'),
    ])
    users.value = u.data
    ownerships.value = o.data
  } catch (e) {
    error.value = errorMessage(e)
  } finally {
    busy.value = false
  }
}
async function perform(fn: () => Promise<unknown>) {
  busy.value = true
  error.value = ''
  try {
    await fn()
    await load()
  } catch (e) {
    error.value = errorMessage(e)
  } finally {
    busy.value = false
  }
}
async function addUser() {
  await perform(() =>
    api.post('/users', {
      ...user.value,
      dingtalk_userid: user.value.dingtalk_userid || null,
    }),
  )
  user.value.password = ''
}
const owners = computed(() => [
  { id: 'admin', label: '管理员', disabled: false },
  ...users.value.map((u) => ({
    id: u.id,
    label: `${u.display_name}（${u.username}）${u.active ? '' : ' · 已停用'}`,
    disabled: !u.active,
  })),
])
const supervisors = computed(() =>
  users.value.filter((u) => u.role === 'manager'),
)
const passwordDialog = ref(false)
const passwordUser = ref<UserAccount | null>(null)
const newPassword = ref('')
const confirmPassword = ref('')
function openPassword(row: UserAccount) {
  passwordUser.value = row
  newPassword.value = ''
  confirmPassword.value = ''
  passwordDialog.value = true
}
function clearPassword() {
  newPassword.value = ''
  confirmPassword.value = ''
  passwordUser.value = null
}
async function changePassword() {
  if (
    !passwordUser.value ||
    newPassword.value.length < 10 ||
    newPassword.value !== confirmPassword.value
  )
    return
  const id = passwordUser.value.id
  await perform(() =>
    api.patch(`/users/${id}`, { password: newPassword.value }),
  )
  if (!error.value) {
    passwordDialog.value = false
    clearPassword()
  }
}
async function editUser(row: UserAccount) {
  await perform(() =>
    api.patch(`/users/${row.id}`, {
      display_name: row.display_name,
      role: row.role,
      active: row.active,
      can_correct: row.can_correct,
      dingtalk_userid: row.dingtalk_userid || null,
    }),
  )
}
async function addScope() {
  await perform(() =>
    api.post('/ownerships', {
      ...scope.value,
      target_id: scope.value.target_id || null,
      supervisor_id: scope.value.supervisor_id || null,
    }),
  )
}
async function saveScope(row: Ownership & { version: number }) {
  await perform(() =>
    api.patch(`/ownerships/${row.id}`, {
      version: row.version,
      owner_id: row.owner_id,
      supervisor_id: row.supervisor_id || null,
      autonomous: row.autonomous,
    }),
  )
}
onMounted(load)
</script>
<template>
  <div class="page-content" v-loading="busy">
    <el-alert v-if="error" :title="error" type="error" :closable="false" />
    <section class="panel table-panel">
      <div class="table-toolbar">
        <div>
          <h2>独立账号</h2>
          <p class="muted">
            停用或修改密码会使旧会话失效。管理员身份不能通过普通账号创建。
          </p>
        </div>
      </div>
      <el-form inline class="coord-form">
        <el-form-item label="账号">
          <el-input v-model="user.username" />
        </el-form-item>
        <el-form-item label="姓名">
          <el-input v-model="user.display_name" />
        </el-form-item>
        <el-form-item label="初始密码（至少10位）">
          <el-input
            v-model="user.password"
            type="password"
            show-password
            autocomplete="new-password"
          />
        </el-form-item>
        <el-form-item label="角色">
          <el-select v-model="user.role" style="width: 120px">
            <el-option label="销售员" value="sales" />
            <el-option label="主管" value="manager" />
          </el-select>
        </el-form-item>
        <el-form-item label="钉钉userid">
          <el-input v-model="user.dingtalk_userid" />
        </el-form-item>
        <el-form-item>
          <el-checkbox v-model="user.can_correct">可批准本人修正</el-checkbox>
        </el-form-item>
        <el-button
          type="primary"
          :disabled="
            !user.username || user.password.length < 10 || !user.display_name
          "
          @click="addUser"
        >
          创建账号
        </el-button>
      </el-form>
      <el-table :data="users" empty-text="尚无独立账号">
        <el-table-column prop="username" label="账号" />
        <el-table-column label="姓名">
          <template #default="{ row }">
            <el-input v-model="row.display_name" />
          </template>
        </el-table-column>
        <el-table-column label="角色">
          <template #default="{ row }">
            <el-select v-model="row.role">
              <el-option label="销售员" value="sales" />
              <el-option label="主管" value="manager" />
            </el-select>
          </template>
        </el-table-column>
        <el-table-column label="绑定钉钉userid" min-width="160">
          <template #default="{ row }">
            <el-input v-model="row.dingtalk_userid" />
          </template>
        </el-table-column>
        <el-table-column label="有效" width="80">
          <template #default="{ row }">
            <el-switch v-model="row.active" />
          </template>
        </el-table-column>
        <el-table-column label="本人修正权限" width="120">
          <template #default="{ row }">
            <el-checkbox v-model="row.can_correct" />
          </template>
        </el-table-column>
        <el-table-column label="操作" width="150">
          <template #default="{ row }">
            <el-button link type="primary" @click="editUser(row)">
              保存
            </el-button>
            <el-button link @click="openPassword(row)">修改密码</el-button>
          </template>
        </el-table-column>
      </el-table>
    </section>
    <section class="panel table-panel">
      <div class="table-toolbar">
        <div>
          <h2>商品归属</h2>
          <p class="muted">
            店铺站点默认归属可由目标 ID
            覆盖；自动托管需单独开启。可按姓名选择负责人和主管。
          </p>
        </div>
      </div>
      <el-form inline class="coord-form">
        <el-form-item label="平台">
          <el-input v-model="scope.platform" />
        </el-form-item>
        <el-form-item label="店铺">
          <el-input v-model="scope.shop_id" />
        </el-form-item>
        <el-form-item label="站点">
          <el-select v-model="scope.site" placeholder="选择站点">
            <el-option
              v-for="site in ['MY', 'PH', 'SG', 'TH', 'VN']"
              :key="site"
              :label="site"
              :value="site"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="目标ID（可选）">
          <el-input v-model="scope.target_id" />
        </el-form-item>
        <el-form-item label="负责人">
          <el-select
            v-model="scope.owner_id"
            filterable
            placeholder="选择负责人"
          >
            <el-option
              v-for="owner in owners"
              :key="owner.id"
              :label="owner.label"
              :disabled="owner.disabled"
              :value="owner.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="主管（可选）">
          <el-select
            v-model="scope.supervisor_id"
            clearable
            filterable
            placeholder="选择主管"
          >
            <el-option
              v-for="manager in supervisors"
              :key="manager.id"
              :disabled="!manager.active"
              :label="`${manager.display_name}（${manager.username}）`"
              :value="manager.id"
            />
          </el-select>
        </el-form-item>
        <el-checkbox v-model="scope.autonomous">自动托管</el-checkbox>
        <el-button
          type="primary"
          :disabled="
            !scope.platform || !scope.shop_id || !scope.site || !scope.owner_id
          "
          @click="addScope"
        >
          添加归属
        </el-button>
      </el-form>
      <el-table
        :data="ownerships"
        empty-text="暂无归属；无负责人条目不会自动提交"
      >
        <el-table-column label="范围" min-width="180">
          <template #default="{ row }">
            {{ row.platform }} / {{ row.shop_id }} / {{ row.site }}
            <p class="muted">{{ row.target_id || '整个店铺站点' }}</p>
          </template>
        </el-table-column>
        <el-table-column label="负责人" min-width="190">
          <template #default="{ row }">
            <el-select v-model="row.owner_id" filterable>
              <el-option
                v-for="owner in owners"
                :key="owner.id"
                :label="owner.label"
                :disabled="owner.disabled"
                :value="owner.id"
              />
            </el-select>
          </template>
        </el-table-column>
        <el-table-column label="主管" min-width="190">
          <template #default="{ row }">
            <el-select v-model="row.supervisor_id" clearable filterable>
              <el-option
                v-for="manager in supervisors"
                :key="manager.id"
                :disabled="!manager.active"
                :label="`${manager.display_name}（${manager.username}）`"
                :value="manager.id"
              />
            </el-select>
          </template>
        </el-table-column>
        <el-table-column label="自动托管" width="105">
          <template #default="{ row }">
            <el-switch v-model="row.autonomous" />
          </template>
        </el-table-column>
        <el-table-column label="操作" width="90">
          <template #default="{ row }">
            <el-button link type="primary" @click="saveScope(row)">
              保存
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </section>
    <el-dialog
      v-model="passwordDialog"
      title="修改账号密码"
      width="420px"
      @closed="clearPassword"
    >
      <p class="muted">
        {{ passwordUser?.display_name }}（{{
          passwordUser?.username
        }}）的新密码保存后，旧会话立即失效。
      </p>
      <el-form label-position="top" @submit.prevent="changePassword">
        <el-form-item label="新密码（至少10位）">
          <el-input
            v-model="newPassword"
            type="password"
            autocomplete="new-password"
            maxlength="256"
          />
        </el-form-item>
        <el-form-item label="再次输入新密码">
          <el-input
            v-model="confirmPassword"
            type="password"
            autocomplete="new-password"
            maxlength="256"
          />
        </el-form-item>
        <el-alert
          v-if="confirmPassword && newPassword !== confirmPassword"
          title="两次输入的密码不一致"
          type="warning"
          :closable="false"
        />
        <el-button @click="passwordDialog = false">取消</el-button>
        <el-button
          native-type="submit"
          type="primary"
          :loading="busy"
          :disabled="newPassword.length < 10 || newPassword !== confirmPassword"
        >
          保存新密码
        </el-button>
      </el-form>
    </el-dialog>
  </div>
</template>
<style scoped>
.coord-form {
  padding: 0 24px 20px;
}
.coord-form :is(.el-input, .el-select) {
  width: 180px;
}
</style>
