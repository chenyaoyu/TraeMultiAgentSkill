from __future__ import annotations

import json
import plistlib
import sqlite3
from pathlib import Path


def create_fixture(
    root: Path,
    *,
    app_name: str = "Trae",
    cli_name: str = "trae",
    bundle_identifier: str = "com.trae.app",
    url_protocol: str = "trae",
    support_dir_name: str = "Trae",
) -> dict[str, Path]:
    app_path = root / f"{app_name}.app"
    cli_dir = app_path / "Contents" / "Resources" / "app" / "bin"
    product_path = app_path / "Contents" / "Resources" / "app" / "product.json"
    main_bundle_path = app_path / "Contents" / "Resources" / "app" / "out" / "main.js"
    ai_chat_bundle_path = (
        app_path
        / "Contents"
        / "Resources"
        / "app"
        / "node_modules"
        / "@byted-icube"
        / "ai-modules-chat"
        / "dist"
        / "index.js"
    )
    info_plist_path = app_path / "Contents" / "Info.plist"
    cli_dir.mkdir(parents=True, exist_ok=True)
    product_path.parent.mkdir(parents=True, exist_ok=True)
    main_bundle_path.parent.mkdir(parents=True, exist_ok=True)
    ai_chat_bundle_path.parent.mkdir(parents=True, exist_ok=True)
    info_plist_path.parent.mkdir(parents=True, exist_ok=True)
    aha_ipc_utils_path = (
        app_path
        / "Contents"
        / "Resources"
        / "app"
        / "node_modules"
        / "@aha-kit"
        / "ipc-darwin-arm64"
        / "dist"
        / "utils.js"
    )
    aha_ipc_utils_path.parent.mkdir(parents=True, exist_ok=True)

    with info_plist_path.open("wb") as handle:
        plistlib.dump(
            {
                "CFBundleDisplayName": app_name,
                "CFBundleIdentifier": bundle_identifier,
                "CFBundleShortVersionString": "3.5.43",
                "CFBundleURLTypes": [
                    {
                        "CFBundleTypeRole": "Viewer",
                        "CFBundleURLName": app_name,
                        "CFBundleURLSchemes": [url_protocol],
                    }
                ],
            },
            handle,
        )
    product_path.write_text(
        json.dumps(
            {
                "appVersion": "3.5.43",
                "urlProtocol": url_protocol,
                "agentShareLinkAuthority": "trae.ai-ide",
                "agent": {"trae": {"SG": "https://coresg-normal.trae.ai"}},
                "iCubeApp": {
                    "authInvalidConfig": {
                        "templateMap": {
                            "20125": {
                                "buttons": [
                                    {"commandId": "update.checkForUpdate"},
                                ]
                            }
                        }
                    },
                    "entitlementModal": {
                        "soloWelcome": {
                            "template": {
                                "buttons": {
                                    "enterprise_flagship": [
                                        {
                                            "commandId": "trae.solo.guide.tryShowSoloGuide",
                                        }
                                    ]
                                }
                            }
                        }
                    },
                    "soloGuide": {
                        "template": {
                            "button": {
                                "try": {"commandId": "soloMode"},
                                "know": {"commandId": "soloBuilder"},
                            }
                        }
                    },
                },
            }
        )
    )

    cli_script = cli_dir / cli_name
    cli_script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "print(json.dumps({'argv': sys.argv[1:], 'cwd': os.getcwd()}))\n"
    )
    cli_script.chmod(0o755)
    main_bundle_path.write_text(
        'import{ahaIpc as R7e}from"electron";'
        'class AY{constructor(t,e){super("ElectronAhaIpcServer",e);const i=R7e.serve(t);this.k=i}}'
        'class LocalClient{constructor(){this.m=new eS({serverName:this.Y,connect:async o=>this.r.ahaIpc.connect(o),logger:this.s})}}'
        'this.g?.("vscode:runAction",{id:"workbench.action.icube.openSettings"});'
        'this.rb("workbench.action.showAboutDialog");'
        '"workbench.action.newWindow";'
        '"workbench.action.switchWindow";'
        '"workbench.action.files.newUntitledFile";'
        '"workbench.action.files.openFolder";'
        '"workbench.action.files.openFileFolder";'
        '"workbench.action.openWorkspace";'
        '"workbench.action.clearRecentFiles";'
        '"workbench.action.toggleDevTools";'
    )
    ai_chat_bundle_path.write_text(
        'class Bk{static{this.SERVER_NAME="chat"}static{this.SERVER_NAME_APPLY="fast_apply"}'
        'static{this.METHODS={Chat:"chat",CreateSession:"create_session",GetSessions:"get_sessions",'
        'CountSessions:"count_sessions",GetMessages:"get_messages",StopChat:"stop_chat"}}}'
        'class BU{static{this.SERVER_NAME="project"}static{this.METHODS={createProject:"create_project"}}}'
        'class FI{static{this.SERVER_NAME="model"}static{this.METHODS={ModelList:"model_list",'
        'ModelListByFunction:"model_list_by_function"}}}'
        'createChatRequestObject(e){let{sceneLocation:t,userMessage:r,isSlashCommandsEnabled:i,'
        'isAppendMessage:n}=e,{sessionId:o,parsedQuery:a,multiMedia:s,modelName:l}=r,'
        '{message:u,mentionContext:d}=xM(a,i,r.autoRuleStats),h=this.getCurrentAgent();'
        'let p=this.getChatRequestAgentType(t,r.agentId),f=this.createChatRequestModelInfo(t,h,l,o),'
        'g=u||r.content,_=this.sessionsStoreService.getSession(o),y=!1,b=a?.filter(e=>xE(e)),'
        'E=b?.map(e=>({command:e.command})),S=this.createModelAutoSelection({modelInfo:f,agent:h,sessionId:o||""}),'
        'w={agent_type:p,agent_id:h?.agent_id,session_id:o,message_id:r.agentMessageId,'
        'mention_context:d,model_name:f.config_name,custom_model:P1(f,["context_window_size"]),'
        'terminal_context:E,message_content:[],code_selections:[],scene_location:t,parsed_query:a,'
        'multi_media:s,agent:h,...S};w.active_text_editor=this._editorFacade.getActiveTextEditor(),'
        'w.workspace_folders=this._workspaceFacade.getCrossPlatformWorkspaceFolderPaths(),'
        'w.is_workspace_folder_changed=y,w.code_selections=[],w.message_content=[],'
        'w.asr_times=r.asrTimes??0,w.is_in_plan_mode=!1,w.is_in_spec_mode=!1;'
        'return w.ask_question_config=this.askQuestionFeatureService.getAskQuestionConfig(),w}'
        'async loadSessionList(){let[e,t,r]=await Promise.all(['
        'this._aiAgentChatService.getSessions({project_id:this.projectId,limit:30,offset:0}),'
        'this._aiAgentChatService.getSessions({project_id:this.projectId,session_type:Dr.ProactiveChat,limit:30,offset:0}),'
        'this._aiAgentChatService.countSessions({project_id:this.projectId}).catch(()=>void 0)])}'
        'async loadSessionMessages(){return this._aiAgentChatService.getSessionMessages('
        '{session_id:e.sessionId,project_id:this.projectId,page_size:20,next_page_token:t})}'
        'async createNewSession(e=!1,t=Dr.SideChat){let r;'
        'let i=await this._aiAgentChatService.createSession({project_id:this._projectStore.getState().projectId,session_type:t});}'
        'let z7={sendToAgentBackground:async(e,t)=>({sessionId:t?.sessionId||"fixture-headless-session",requestMessageId:"fixture-request-id",'
        'unstableFields:{session:{sessionId:t?.sessionId||"fixture-headless-session",messages:[{role:"user",content:e[0]},{role:"assistant",content:"fixture headless answer"}]}}})};'
        'let ur={getInstance:()=>({resolve:()=>({getCurrentLoginStatusSync:()=>"marscode"})})},Sn={IICubeAuthService:"auth"};'
        'let i={registerCommand:function(){}};'
        'i.registerCommand("workbench.action.chat.icube.send.codeReview",async(e,t,r)=>{return await z7.sendToAgentBackground(t,{enableUnstableFields:!0})});'
    )
    aha_ipc_utils_path.write_text(
        "const AHA_IPC_DIR = 'aha';\n"
        "function generateIpcAddress(name, runtimeDir) {\n"
        "  const ahaDir = runtimeDir ? `${runtimeDir}/${AHA_IPC_DIR}` : `/tmp/${AHA_IPC_DIR}`;\n"
        "  const safeName = name.replace(/[^a-zA-Z0-9._-]/g, '_');\n"
        "  const socketPath = `${ahaDir}/${safeName}.sock`;\n"
        "  return `ipc://${socketPath}`;\n"
        "}\n"
    )

    support_dir = root / "Library" / "Application Support" / support_dir_name
    user_data_dir = root / ".trae"
    global_storage = support_dir / "User" / "globalStorage"
    global_storage.mkdir(parents=True, exist_ok=True)
    user_data_dir.mkdir(parents=True, exist_ok=True)

    storage_json = {
        "iCubeAuthInfo://icube.cloudide": json.dumps(
            {
                "token": "redacted-token",
                "refreshToken": "redacted-refresh-token",
                "expiredAt": "2026-04-16T08:52:20.397Z",
                "refreshExpiredAt": "2026-09-29T08:52:20.397Z",
                "userId": "7594301358220624917",
                "host": "https://api-sg-central.trae.ai",
                "userRegion": {"region": "SG"},
                "account": {
                    "username": "Fixture User",
                    "email": "fixture@example.com",
                    "loginScope": "trae",
                    "scope": "marscode",
                },
            }
        )
    }
    (global_storage / "storage.json").write_text(json.dumps(storage_json))

    recent_folder = (root / "recent-folder").resolve()
    recent_folder.mkdir(parents=True, exist_ok=True)
    recent_workspace_file = (root / "recent.code-workspace").resolve()
    recent_workspace_file.write_text(
        json.dumps({"folders": [{"path": str(recent_folder)}]})
    )
    recent_file = (recent_folder / "recent-note.txt").resolve()
    recent_file.write_text("fixture recent file")

    db_path = global_storage / "state.vscdb"
    connection = sqlite3.connect(str(db_path))
    try:
        connection.execute("create table ItemTable(key text primary key, value text)")
        connection.executemany(
            "insert into ItemTable(key, value) values (?, ?)",
            [
                (
                    "7594301358220624917_AI.agent.model.selected_model",
                    json.dumps(
                        {
                            "name": "bigmodel-plan//glm-4.7",
                            "display_name": "GLM-4.7",
                            "provider": "bigmodel-plan",
                            "model_type": "chat_model",
                            "config_source": 3,
                            "selectable": True,
                            "ak": "fixture-ak",
                            "sk": "fixture-sk",
                            "custom_config": "{\"temperature\":0.1}",
                        }
                    ),
                ),
                (
                    "7594301358220624917_AI.agent.mode",
                    json.dumps(
                        [
                            {
                                "type": 1,
                                "status": False,
                                "defaultStatus": True,
                            }
                        ]
                    ),
                ),
                (
                    "7594301358220624917_AI.agent.model.model_list",
                    json.dumps(
                        [
                            {
                                "name": "gpt-5.3-codex",
                                "display_name": "GPT-5.3 Codex",
                                "provider": None,
                                "config_source": 1,
                                "model_type": "reasoning_model",
                                "is_default": True,
                                "selectable": True,
                            },
                            {
                                "name": "bigmodel-plan//glm-4.7",
                                "display_name": "GLM-4.7",
                                "provider": "bigmodel-plan",
                                "config_source": 3,
                                "model_type": "chat_model",
                                "selectable": True,
                                "ak": "fixture-ak",
                                "sk": "fixture-sk",
                                "custom_config": "{\"temperature\":0.1}",
                            },
                        ]
                    ),
                ),
                (
                    "7594301358220624917_AI.agent.model.model_list_map",
                    json.dumps(
                        {
                            "builder": [
                                {
                                    "name": "gpt-5.3-codex",
                                    "display_name": "GPT-5.3 Codex",
                                    "provider": None,
                                    "config_source": 1,
                                    "model_type": "reasoning_model",
                                    "is_default": True,
                                    "selectable": True,
                                },
                                {
                                    "name": "bigmodel-plan//glm-4.7",
                                    "display_name": "GLM-4.7",
                                    "provider": "bigmodel-plan",
                                    "config_source": 3,
                                    "model_type": "chat_model",
                                    "selectable": True,
                                    "ak": "fixture-ak",
                                    "sk": "fixture-sk",
                                    "custom_config": "{\"temperature\":0.1}",
                                },
                            ],
                            "solo_coder": [
                                {
                                    "name": "glm-4.7-solo",
                                    "display_name": "GLM-4.7 SOLO",
                                    "provider": None,
                                    "config_source": 1,
                                    "model_type": "reasoning_model",
                                    "is_default": True,
                                    "selectable": True,
                                },
                                {
                                    "name": "gpt-5.3-codex",
                                    "display_name": "GPT-5.3 Codex",
                                    "provider": None,
                                    "config_source": 1,
                                    "model_type": "reasoning_model",
                                    "selectable": True,
                                },
                            ],
                            "solo_builder": [
                                {
                                    "name": "gpt-4.1-ui",
                                    "display_name": "GPT-4.1 UI",
                                    "provider": None,
                                    "config_source": 1,
                                    "model_type": "chat_model",
                                    "is_default": True,
                                    "selectable": True,
                                }
                            ],
                        }
                    ),
                ),
                (
                    "7594301358220624917_ai-chat:sessionRelation:globalModelMap",
                    json.dumps({"dev_builder": "1_-_gpt-5.3-codex"}),
                ),
                ("workbench.global.soloMode.enabled", "0"),
                (
                    "workbench.global.soloMode.info",
                    json.dumps(
                        {
                            "currentSoloTabId": "",
                            "isVisibleExtensionView": True,
                        }
                    ),
                ),
                ("chat.ChatSessionStore.index", json.dumps({"version": 1, "entries": {}})),
                ("chat.workspaceTransfer", "[]"),
                (
                    "history.recentlyOpenedPathsList",
                    json.dumps(
                        {
                            "entries": [
                                {
                                    "folderUri": recent_folder.as_uri(),
                                },
                                {
                                    "workspace": {
                                        "id": "fixture-recent-workspace",
                                        "configPath": recent_workspace_file.as_uri(),
                                    }
                                },
                                {
                                    "fileUri": recent_file.as_uri(),
                                    "label": "recent-note.txt",
                                },
                                {
                                    "folderUri": "vscode-remote://ssh-remote%2Bfixture/home/test/repo",
                                    "label": "~/repo [SSH: fixture]",
                                    "remoteAuthority": "ssh-remote+fixture",
                                },
                            ]
                        }
                    ),
                ),
                ("all_session_badges_session-alpha", "{}"),
                (
                    "all_session_badges_session-beta",
                    json.dumps({"badge": "attention"}),
                ),
            ],
        )
        connection.commit()
    finally:
        connection.close()

    mcp_cache = global_storage / ".mcp_gallery_cache"
    mcp_cache.mkdir(parents=True, exist_ok=True)
    (mcp_cache / "executeautomation.mcp-playwright.json").write_text(
        json.dumps(
            {
                "id": "executeautomation.mcp-playwright",
                "displayName": "Playwright",
                "repository": "https://github.com/executeautomation/mcp-playwright",
                "version": "2025.03.24_1510",
                "mcpServerType": "stdio",
                "commands": {
                    "universal": {
                        "run": [
                            {
                                "command": "npx",
                                "args": ["-y", "@executeautomation/playwright-mcp-server"],
                            }
                        ]
                    }
                },
            }
        )
    )

    modular_dir = support_dir / "ModularData"
    (modular_dir / "ckg_server").mkdir(parents=True, exist_ok=True)
    (modular_dir / "ckg_server" / "local_env.json").write_text(
        json.dumps(
            {
                "host": "",
                "device_id": "7592502265287607825",
                "is_privacy_mode": False,
                "host_map": {
                    "7594301358220624917": "https://coresg-normal.trae.ai",
                    "default": "",
                },
            }
        )
    )
    (modular_dir / "ckg_server" / "env_codekg.db").write_bytes(b"opaque-ckg-store")

    sandbox_dir = modular_dir / "ai-agent" / "sandbox"
    sandbox_dir.mkdir(parents=True, exist_ok=True)
    (modular_dir / "ai-agent" / "database.db").write_bytes(b"opaque-ai-agent-store")
    (sandbox_dir / "fixture-session.json").write_text(
        json.dumps(
            {
                "name": "fixture-session",
                "permission": [
                    {"file_inherit_user": "/Users/example/Projects/fixture"},
                    {"file_inherit_user": "/private/tmp"},
                ],
            }
        )
    )

    latest_log_dir = support_dir / "logs" / "20260404T120000"
    (latest_log_dir / "Modular").mkdir(parents=True, exist_ok=True)
    (latest_log_dir / "window1").mkdir(parents=True, exist_ok=True)
    (latest_log_dir / "window2" / "exthost" / "cloudide.icube-remote-ssh").mkdir(
        parents=True, exist_ok=True
    )
    (latest_log_dir / "main.log").write_text(
        "\n".join(
            [
                "2026-04-04T12:00:00.000+08:00 [info] [AhaIpcServer] ElectronAhaIpcServer registerService BootService",
                "2026-04-04T12:00:00.000+08:00 [info] [AhaRpcClient] ahaIpc connect serverName: ai-agent",
                '2026-04-04T12:00:00.010+08:00 [info] ai-agent start options {"ENABLE_IPC_SERVER":"true","AHA_IPC_SERVICE_NAME":"ai-agent"}',
                '2026-04-04T12:00:00.020+08:00 [info] [ICubeProcessManager] doRequest, data: {"packet_type":"request","session_id":"","channel_id":"046fb81a-fe80-4bfe-909b-1110e8628ab7","params":{"service":"healthcheck","method":"ping","data":"","common_params":{},"user_info":{"user_id":"fixture-user"},"streamlined_common_params":{},"client_info":{"connect_session_id":""}}}',
                '2026-04-04T12:00:00.030+08:00 [info] [ICubeProcessManager] doRequest, response: {"message":"success","code":0,"data":{"message":"pong"}}',
                "2026-04-04T12:00:00.040+08:00 [info] SupabaseOAuthLocalServer#Found available port: 17790",
                "main log line 1",
                "main log line 2",
            ]
        )
        + "\n"
    )
    (latest_log_dir / "Modular" / "ai-agent_0_fixture_stdout.log").write_text(
        "\n".join(
            [
                "INFO Updated model config cache for user: 1, env: , function: chat_v3, configs count: 28",
                "INFO Updated model config cache for user: 1, env: , function: builder_v3, configs count: 29",
                "2026-04-04T12:00:00.050+08:00 [info] [aha_ipc] new FFI connection accepted",
                "2026-04-04T12:00:00.060+08:00 [info] [aha_ipc] jsonrpsee server started, waiting for stop...",
                '2026-04-04T12:00:00.070+08:00 [info] [CKGClient] Lookup CKG Server Addr : "127.0.0.1:51002"',
                '2026-04-04T12:00:00.080+08:00 [info] upstream response headers: {"content-type":"application/grpc"}',
                '2026-04-04T12:00:00.090+08:00 [info] process_ipc_request called!, channel_id: 046fb81a-fe80-4bfe-909b-1110e8628ab7, session_id: "", service: "healthcheck", method: "ping"',
                '2026-04-04T12:00:00.110+08:00  INFO process_ipc_request:route:chat: ai_agent::domain::plan::simple_service_v2: first token flushed thought, thought="fixture-first-token", reasoning=Some("") trace_id="fixture-trace-id" session_id=fixture-session-id task_id=fixture-task-id message_id=fixture-backend-message-id session_id=fixture-session-id',
                '2026-04-04T12:00:00.120+08:00  WARN process_ipc_request:route:chat: ai_agent::infrastructure::adapter::llm::event: Unknown event: Event { event: "progress_notice", data: "\\"Processing_1\\"", id: "1", retry: None } trace_id="fixture-trace-id" session_id=fixture-session-id task_id=fixture-task-id message_id=fixture-backend-message-id session_id=fixture-session-id',
                '2026-04-04T12:00:00.130+08:00  INFO process_ipc_request:route:chat: ai_agent::domain::plan::simple_service_v2: plan final token cost: 237ms trace_id="fixture-trace-id" session_id=fixture-session-id task_id=fixture-task-id message_id=fixture-backend-message-id session_id=fixture-session-id',
                '2026-04-04T12:00:00.140+08:00  INFO process_ipc_request:route:chat: ai_agent::domain::chat::chat_context_entity: [ChatContextEntity] chat finished trace_id="fixture-trace-id" session_id=fixture-session-id task_id=fixture-task-id message_id=fixture-backend-message-id session_id=fixture-session-id',
                '2026-04-04T12:00:00.150+08:00  INFO process_ipc_request:route:chat_stopped_handle:chat_turn_finish: ai_agent::domain::snapshot::snapshot_service: [snapshot_v2][fixture-sandbox] chat_turn_finish session_id: fixture-session-id, message_id: "fixture-message-id" trace_id="fixture-trace-id" session_id=fixture-message-id',
                '2026-04-04T12:00:00.200+08:00  INFO process_ipc_request: ai_agent::infrastructure::towel::rpc: [RPC] process_ipc_request called!, channel_id: fixture-empty-channel, trace_id: fixture-empty-trace, service: "chat", method: "get_messages" trace_id="fixture-empty-trace"',
                '2026-04-04T12:00:00.201+08:00  INFO process_ipc_request:route: ai_agent::handler: route: service:"chat", method:"get_messages", connect_session_id:"fixture-empty-connect" trace_id="fixture-empty-trace"',
                '2026-04-04T12:00:00.202+08:00  INFO process_ipc_request:route:get_multi_by_session_id: ai_agent::domain::chat_message::repository: fast_convert_chat_message_models: count=0, actual=0, duration=12.416µs trace_id="fixture-empty-trace"',
                '2026-04-04T12:00:00.203+08:00  INFO process_ipc_request:route: ai_agent::domain::chat::service: [ChatService] get messages 0 trace_id="fixture-empty-trace"',
                '2026-04-04T12:00:00.204+08:00  INFO process_ipc_request:route: ai_agent::domain::chat::service: [ChatService] get turns 0 trace_id="fixture-empty-trace"',
                '2026-04-04T12:00:00.205+08:00  INFO process_ipc_request:route: ai_agent::domain::chat::server_service: [build_server_history_ids_cache] START: session_id=fixture-empty-session trace_id="fixture-empty-trace"',
                '2026-04-04T12:00:00.206+08:00  INFO process_ipc_request:route: ai_agent::handler: route end: response_size_bytes: Some(230) trace_id="fixture-empty-trace"',
                '2026-04-04T12:00:00.207+08:00  INFO ai_agent::domain::chat::server_service: Prepared server history ids: []',
                '2026-04-04T12:00:00.300+08:00  INFO process_ipc_request: ai_agent::infrastructure::towel::rpc: [RPC] process_ipc_request called!, channel_id: fixture-error-channel, trace_id: fixture-error-trace, service: "chat", method: "get_messages" trace_id="fixture-error-trace"',
                '2026-04-04T12:00:00.301+08:00  INFO process_ipc_request:route: ai_agent::handler: route: service:"chat", method:"get_messages", connect_session_id:"fixture-error-connect" trace_id="fixture-error-trace"',
                '2026-04-04T12:00:00.302+08:00  INFO process_ipc_request:route:get_multi_by_session_id: ai_agent::domain::chat_message::repository: fast_convert_chat_message_models: count=4, actual=4, duration=15.274791ms trace_id="fixture-error-trace"',
                '2026-04-04T12:00:00.303+08:00  INFO process_ipc_request:route: ai_agent::domain::chat::service: [ChatService] get messages 4 trace_id="fixture-error-trace"',
                '2026-04-04T12:00:00.304+08:00  INFO process_ipc_request:route: ai_agent::domain::chat::service: [ChatService] get turns 2 trace_id="fixture-error-trace"',
                '2026-04-04T12:00:00.305+08:00  INFO process_ipc_request:route: ai_agent::domain::chat::server_service: [build_server_history_ids_cache] START: session_id=fixture-error-session trace_id="fixture-error-trace"',
                '2026-04-04T12:00:00.306+08:00  INFO process_ipc_request:route: ai_agent::handler: route end: response_size_bytes: Some(2048) trace_id="fixture-error-trace"',
                '2026-04-04T12:00:00.307+08:00  INFO ai_agent::domain::chat::server_service: Prepared server history ids: ["hist-error-1", "hist-error-2", "hist-error-3"]',
                '2026-04-04T12:00:00.308+08:00  INFO ai_agent::infrastructure::adapter::cloud_agent: REQUEST BODY: GetHistoryStateRequest { history_id_list: ["hist-error-1", "hist-error-2", "hist-error-3"] }',
                '2026-04-04T12:00:00.309+08:00  INFO ai_agent::infrastructure::common::http: [HTTPClient] request url https://coresg-normal.trae.ai/api/agent/v3/query_history_state',
                '2026-04-04T12:00:00.310+08:00  INFO ai_agent::infrastructure::aha_net::aha_net_client: [AhaNetHTTPClient] url https://coresg-normal.trae.ai/api/agent/v3/query_history_state, response_headers: {"content-type":"application/json; charset=utf-8","content-length":"43","x-request-id":"req-fixture-error"}',
                '2026-04-04T12:00:00.311+08:00  WARN ai_agent::domain::chat::server_service: Get history state error: http request error: status_code 200, invalid type: null, expected a sequence at line 1 column 15',
                '2026-04-04T12:00:00.400+08:00  INFO process_ipc_request: ai_agent::infrastructure::towel::rpc: [RPC] process_ipc_request called!, channel_id: fixture-ok-channel, trace_id: fixture-ok-trace, service: "chat", method: "get_messages" trace_id="fixture-ok-trace"',
                '2026-04-04T12:00:00.401+08:00  INFO process_ipc_request:route: ai_agent::handler: route: service:"chat", method:"get_messages", connect_session_id:"fixture-ok-connect" trace_id="fixture-ok-trace"',
                '2026-04-04T12:00:00.402+08:00  INFO process_ipc_request:route:get_multi_by_session_id: ai_agent::domain::chat_message::repository: fast_convert_chat_message_models: count=6, actual=6, duration=9.1ms trace_id="fixture-ok-trace"',
                '2026-04-04T12:00:00.403+08:00  INFO process_ipc_request:route: ai_agent::domain::chat::service: [ChatService] get messages 6 trace_id="fixture-ok-trace"',
                '2026-04-04T12:00:00.404+08:00  INFO process_ipc_request:route: ai_agent::domain::chat::service: [ChatService] get turns 3 trace_id="fixture-ok-trace"',
                '2026-04-04T12:00:00.405+08:00  INFO process_ipc_request:route: ai_agent::domain::chat::server_service: [build_server_history_ids_cache] START: session_id=fixture-ok-session trace_id="fixture-ok-trace"',
                '2026-04-04T12:00:00.406+08:00  INFO process_ipc_request:route: ai_agent::handler: route end: response_size_bytes: Some(4096) trace_id="fixture-ok-trace"',
                '2026-04-04T12:00:00.407+08:00  INFO ai_agent::domain::chat::server_service: Prepared server history ids: ["hist-ok-1", "hist-ok-2"]',
                '2026-04-04T12:00:00.408+08:00  INFO ai_agent::infrastructure::adapter::cloud_agent: REQUEST BODY: GetHistoryStateRequest { history_id_list: ["hist-ok-1", "hist-ok-2"] }',
                '2026-04-04T12:00:00.409+08:00  INFO ai_agent::infrastructure::common::http: [HTTPClient] request url https://coresg-normal.trae.ai/api/agent/v3/query_history_state',
                '2026-04-04T12:00:00.410+08:00  INFO ai_agent::infrastructure::aha_net::aha_net_client: [AhaNetHTTPClient] url https://coresg-normal.trae.ai/api/agent/v3/query_history_state, response_headers: {"content-type":"application/json; charset=utf-8","content-length":"94","x-request-id":"req-fixture-ok"}',
            ]
        )
    )
    (latest_log_dir / "Modular" / "ckg_0_fixture_stdout.log").write_text(
        "2026-04-04T12:00:00.000+08:00 [info] server start at 51002\n"
    )
    (latest_log_dir / "window1" / "renderer.log").write_text(
        "\n".join(
            [
                "2026-04-04T12:00:00.000+08:00 [info] [TransportManager] executeRequest, project create_project, "
                "11111111-1111-4111-8111-111111111111, cost: 0",
                "2026-04-04T12:00:00.010+08:00 [info] [TransportManager] executeRequest success, project create_project, "
                "11111111-1111-4111-8111-111111111111, cost: 10",
                '2026-04-04T12:00:00.020+08:00 [info] [ai-chat][ai-chat] initChatView createProject result: '
                '{"project_id":"fixture-project","real_project_id":"fixture-project"}',
                "2026-04-04T12:00:00.030+08:00 [info] [TransportManager] executeRequest, chat get_sessions, "
                "22222222-2222-4222-8222-222222222222, cost: 0",
                "2026-04-04T12:00:00.040+08:00 [info] [TransportManager] executeRequest success, chat get_sessions, "
                "22222222-2222-4222-8222-222222222222, cost: 14",
                "2026-04-04T12:00:00.050+08:00 [info] [TransportManager] executeRequest, chat create_session, "
                "33333333-3333-4333-8333-333333333333, cost: 0",
                "2026-04-04T12:00:00.060+08:00 [info] [TransportManager] executeRequest success, chat create_session, "
                "33333333-3333-4333-8333-333333333333, cost: 16",
                "2026-04-04T12:00:00.070+08:00 [info] [TransportManager] executeRequest, chat chat, "
                "44444444-4444-4444-8444-444444444444, cost: 0",
                '2026-04-04T12:00:00.080+08:00 [info] [ai-chat][SessionService] switchToSession '
                'setSwitchSessionLoading(true) {"sessionId":"fixture-session-id"}',
                "2026-04-04T12:00:00.090+08:00 [info] [ai-chat][chatStreamService] chatStream handleStream sessionId: "
                "fixture-session-id",
                '2026-04-04T12:00:00.100+08:00 [info] [ai-chat][ai-chat][☕] event:  code_comp_trigger ; params:  '
                '{"chat_model":"GLM-4.7","session_id":"fixture-session-id","message_id":"fixture-message-id",'
                '"agent_type":"solo_coder"}',
                '2026-04-04T12:00:00.110+08:00 [info] [ai-chat][ai-chat][☕] event:  tool_call_show ; params:  '
                '{"chat_model":"GLM-4.7","session_id":"fixture-session-id","message_id":"fixture-message-id",'
                '"agent_type":"solo_coder","tool_type":"view_files","tool_id":"fixture-tool-1"}',
                '2026-04-04T12:00:00.120+08:00 [info] [ai-chat][ai-chat][☕] event:  run_script_show ; params:  '
                '{"chat_model":"GLM-4.7","session_id":"fixture-session-id","message_id":"fixture-message-id",'
                '"agent_type":"solo_coder","block_type":"run_script","tool_id":"fixture-tool-2"}',
                '2026-04-04T12:00:00.130+08:00 [info] [ai-chat][ai-chat][☕] event:  run_script_success ; params:  '
                '{"chat_model":"GLM-4.7","session_id":"fixture-session-id","message_id":"fixture-message-id",'
                '"agent_type":"solo_coder","block_type":"run_script","tool_id":"fixture-tool-2","runtime_duration":1234}',
                '2026-04-04T12:00:00.135+08:00 [info] [ToolingTerminalTrace]toolcall_run_command_tracing '
                '{"categories":{"tool_call_key":"fixture-tool-2","chat_session_id":"fixture-session-id","error":"0",'
                '"terminal_type":"zsh","is_reused":"true","blocking":"true","command":"echo fixture",'
                '"terminal_instance_id":"7","exitCode":"0","detectionStrategy":"onCommandFinishedNoMarker",'
                '"last_stage":"command_finished"},"metrics":{"total_duration":321}}',
                '2026-04-04T12:00:00.136+08:00 [info] [tooling] fixture-trace commandId:fixture-command result: '
                'exitCode=0 commandResult= [{"serverCallId":"fixture-tool-2","terminalId":7,"createTime":1775352000136,'
                '"command":"echo fixture","exitCode":0,"logs":["^[]11;rgb:1a1a/1b1b/1d1d^[\\\\$ echo fixture","^[[19;1Rfixture"],'
                '"detectionStrategy":"onCommandFinishedNoMarker"}]',
                '2026-04-04T12:00:00.140+08:00 [info] [ai-chat][ai-chat][☕] event:  code_comp_complete_shown ; params:  '
                '{"chat_model":"GLM-4.7","session_id":"fixture-session-id","message_id":"fixture-message-id",'
                '"agent_type":"solo_coder","request_round_count":2,"tool_count":2,"duration":4567,"is_interrupted":0}',
            ]
        )
    )
    (
        latest_log_dir
        / "window2"
        / "exthost"
        / "cloudide.icube-remote-ssh"
        / "Remote - SSH(TRAE).log"
    ).write_text(
        "[12:00:00] [AhaIpcServer] NodeAhaIpcServer [AhaIPC] [client] start, routingId:  "
        "700622a7-bc00-43d1-8959-936c7308ff05 , server:  ai-agent , ipc address:  "
        "ipc:///home/test/.trae-server/socks/2061289/e7f53dc/aha/ai-agent.sock\n"
    )
    (support_dir / "1.10-main.sock").write_text("")

    return {
        "app_path": app_path,
        "support_dir": support_dir,
        "user_data_dir": user_data_dir,
    }
