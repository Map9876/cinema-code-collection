import requests
import random
import time
import json
import pandas as pd
from tqdm import tqdm
from queue import Queue
from threading import Thread, Lock
import logging
from fake_useragent import UserAgent

# 配置日志记录
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('cinema_scraper.log'),
        logging.StreamHandler()
    ]
)

class SmartController:
    """智能速率控制器"""
    def __init__(self):
        self.last_success_time = time.time()
        self.request_interval = 0.3  # 初始请求间隔
        self.timeout_count = 0
        self.error_count = 0
        self.lock = Lock()
        self.ua = UserAgent()
        
    def get_headers(self):
        """动态生成请求头"""
        return {
            'User-Agent': self.ua.random,
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'zh-CN,zh;q=0.9',
            'Content-Type': 'application/x-www-form-urlencoded',
            'Origin': 'https://ys.endata.cn',
            'Referer': 'https://ys.endata.cn/Details/Cinema',
        }
    
    def adjust_speed(self, success):
        """根据请求结果动态调整速度"""
        with self.lock:
            now = time.time()
            if success:
                self.timeout_count = max(0, self.timeout_count - 1)
                self.error_count = max(0, self.error_count - 0.5)
                
                # 如果连续成功，适当加快速度
                if now - self.last_success_time < 0.5:
                    self.request_interval = max(0.05, self.request_interval * 0.9)
            else:
                self.timeout_count += 1
                self.error_count += 1
                
                # 根据错误频率动态调整
                if self.timeout_count > 2:
                    self.request_interval = min(5.0, self.request_interval * 1.5)
                
                # 如果错误太多，暂停一会儿
                if self.error_count > 10:
                    wait_time = min(60, 5 * self.error_count)
                    logging.warning(f"Too many errors, sleeping for {wait_time} seconds")
                    time.sleep(wait_time)
                    self.error_count = 0
            
            self.last_success_time = now
            return self.request_interval

class CinemaScraper:
    def __init__(self):
        self.controller = SmartController()
        self.session = requests.Session()
        self.session.mount('https://', requests.adapters.HTTPAdapter(
            max_retries=3,
            pool_connections=10,
            pool_maxsize=20
        ))
        self.print_lock = Lock()
        self.data_lock = Lock()
        
    def get_cinema_data(self, cinemaid, retry=3):
        """获取单个影院数据"""
        for attempt in range(retry):
            try:
                # 动态等待
                wait_time = self.controller.adjust_speed(True)
                time.sleep(wait_time)
                
                response = self.session.post(
                    'https://ys.endata.cn/enlib-api/api/cinema/getcinema_baseinfo_byid.do',
                    headers=self.controller.get_headers(),
                    data={
                        'r': str(random.random()),
                        'cinemaid': str(cinemaid)
                    },
                    timeout=(3.05, 30)
                )
                
                response.raise_for_status()
                json_data = response.json()
                
                if json_data.get('status') == 1 and json_data.get('data', {}).get('table0'):
                    return json_data['data']['table0'][0]
                
                return None
                
            except requests.exceptions.RequestException as e:
                self.controller.adjust_speed(False)
                if attempt == retry - 1:
                    logging.warning(f"Failed cinema ID {cinemaid}: {str(e)}")
                    raise
                
                # 指数退避等待
                wait_time = (2 ** attempt) + random.random()
                time.sleep(wait_time)
            except Exception as e:
                self.controller.adjust_speed(False)
                logging.error(f"Unexpected error with ID {cinemaid}: {str(e)}")
                return None
    
    def worker(self, queue, results, errors, pbar):
        """工作线程函数"""
        while True:
            try:
                cinemaid = queue.get_nowait()
                
                try:
                    data = self.get_cinema_data(cinemaid)
                    if data:
                        with self.data_lock:
                            results.append(data)
                    
                    with self.print_lock:
                        pbar.set_description(f"Last ID: {cinemaid}, Found: {len(results)}")
                        pbar.update(1)
                        
                except Exception as e:
                    with self.data_lock:
                        errors.append({
                            'cinemaid': cinemaid,
                            'error': str(e),
                            'timestamp': time.strftime('%Y-%m-%d %H:%M:%S')
                        })
                
                finally:
                    queue.task_done()
                    
            except Exception as e:
                break
    
    def run(self, start_id=1, end_id=50000, max_threads=5):
        """运行爬虫"""
        all_cinemas = []
        errors = []
        task_queue = Queue()
        
        # 填充任务队列
        for cinemaid in range(start_id, end_id + 1):
            task_queue.put(cinemaid)
        
        # 初始化进度条
        with tqdm(total=end_id - start_id + 1, desc="Scraping Progress") as pbar:
            # 启动工作线程
            threads = []
            for _ in range(max_threads):
                t = Thread(
                    target=self.worker,
                    args=(task_queue, all_cinemas, errors, pbar),
                    daemon=True
                )
                t.start()
                threads.append(t)
            
            # 监控线程状态
            try:
                while any(t.is_alive() for t in threads):
                    time.sleep(1)
                    # 每小时自动保存一次进度
                    if int(time.time()) % 3600 == 0:
                        self.save_results(all_cinemas, errors)
                
                task_queue.join()
                
            except KeyboardInterrupt:
                logging.info("Received keyboard interrupt, stopping threads...")
                while not task_queue.empty():
                    task_queue.get()
                    task_queue.task_done()
                
                for t in threads:
                    if t.is_alive():
                        t.join(timeout=1)
            
            finally:
                self.save_results(all_cinemas, errors)
                logging.info(f"Scraping completed. Found {len(all_cinemas)} cinemas, {len(errors)} errors.")
    
    def save_results(self, all_cinemas, errors):
        """保存结果到文件"""
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        
        try:
            # 保存完整数据
            if all_cinemas:
                df_full = pd.DataFrame(all_cinemas)
                df_full.to_excel(f'results/all_cinemas_data_{timestamp}.xlsx', index=False)
                df_full.to_json(f'results/all_cinemas_data_{timestamp}.json', orient='records', force_ascii=False)
                
                # 保存简略数据
                df_simple = pd.DataFrame([{
                    'CinemaID': x.get('CinemaID'),
                    'CinemaName': x.get('CinemaName'),
                    'ZZID': x.get('ZZID'),
                    'Province': x.get('ProvinceName'),
                    'City': x.get('CityName')
                } for x in all_cinemas])
                df_simple.to_excel(f'results/cinema_name_zzid_{timestamp}.xlsx', index=False)

                simple_data = [{
                                "CinemaName": x.get("CinemaName"),
                                "ZZID": x.get("ZZID"),
                                "CinemaID": x.get("CinemaID")
                            } for x in all_cinemas]
                            
                with open(f'results/cinema_simple_{timestamp}.json', 'w', encoding='utf-8') as f:
                    json.dump(simple_data, f, ensure_ascii=False, indent=2)
            
            # 保存错误日志
            if errors:
                df_errors = pd.DataFrame(errors)
                df_errors.to_excel(f'results/error_logs_{timestamp}.xlsx', index=False)
            
            logging.info(f"Results saved at {timestamp}")
        
        except Exception as e:
            logging.error(f"Failed to save results: {str(e)}")

if __name__ == '__main__':
    import os
    os.makedirs('results', exist_ok=True)
    
    scraper = CinemaScraper()
    
    # 配置参数
    config = {
        'start_id': 1,         # 起始ID
        'end_id': 50000,       # 结束ID (预估范围)
        'max_threads': 5       # 并发线程数
    }
    
    logging.info("Starting cinema scraper with config: %s", config)
    scraper.run(**config)



